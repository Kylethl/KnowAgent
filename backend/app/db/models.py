# ORM 模型（P1-DB1 / P1-DB2 / P3-DB2 / P6-DB1 / P6-DB2）
# ---------------------------------------------------------------------
# 与 backend/sql/ 下的建表脚本字段一一对应；
# 该 SQL 文件为「建表规范源头」，本文件仅用于应用层读写。
#
# 表清单：
#   users / conversations / messages（001）
#   documents（002 + 003 审核字段 + 006 多模态解析字段）
#   document_chunks（004，pgvector 向量）
#   notifications（007，P6 审核通知）
#   feedback（008，P6 答案反馈）
#
# 约定：
#   - 主键 UUID 默认由 PG 内置 gen_random_uuid() 生成（server_default）。
#   - 时间字段使用带时区的 TIMESTAMPTZ（DateTime(timezone=True)）。
#   - 与 SQL 文件保持一致，在表级别补充 role 的 CHECK 约束，
#     保证基于 ORM 的 create_all 也能生成等价结构。

from typing import List, Optional
from uuid import UUID

from sqlalchemy import (
    Boolean,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pgvector.sqlalchemy import Vector

from app.db.engine import Base

# 向量维度：必须与 sql/004_p3_db2_vector.sql 中 vector(N) 的 N 一致，
# 也与 config.EMBEDDING_DIM 对齐（qwen3-vl-embedding 取 1024）。
# ⚠️ 三者任一变更都要同步，否则写入时会被 PG 以维度不匹配拒绝。
EMBEDDING_DIM = 1024


class User(Base):
    """用户表（P2-B2 业务表；P1-DB1 作为 conversations 外键前置建立）。"""

    __tablename__ = "users"

    # 主键：PG 内置 gen_random_uuid() 生成
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 登录名，唯一且非空
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 邮箱，唯一但可空
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    # 密码哈希（bcrypt 等），绝不存明文
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 角色：user / admin（P2 RBAC 用）
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'user'"))
    # 账号是否启用（P2 软封禁用）
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # 创建时间（服务端时区）
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 更新时间；onupdate 使每次 UPDATE 自动刷新为当前时间
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # 关联关系（仅 ORM 层，便于查询，不改变表结构）
    conversations: Mapped[List["Conversation"]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role!r}>"


class Conversation(Base):
    """会话表（P1 核心）：一个用户可有多轮对话，互相隔离。"""

    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 所属用户；用户被删时会话级联删除
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 会话标题（可首条问题摘要自动生成）
    title: Mapped[Optional[str]] = mapped_column(String(255))
    # 模型字段：与后端 config.DEEPSEEK_MODEL 对齐（DeepSeek-Flash）
    model: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'deepseek-flash'")
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # 软删除标记：非空表示已删除（P2 删除功能使用）
    deleted_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))

    # 关联：所属用户 + 本会话下的消息
    user: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[List["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",  # 删除会话时一并删除其消息
    )

    def __repr__(self) -> str:
        return f"<Conversation id={self.id} user_id={self.user_id} model={self.model!r}>"


class Message(Base):
    """消息表（P1 核心）：会话内的单条消息，先存文本，P4 追加引用。"""

    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 所属会话；会话被删时消息级联删除
    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 角色：user / assistant / system
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # 消息正文
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 令牌数，上下文窗口管理（P2）用
    token_count: Mapped[Optional[int]] = mapped_column(Integer)
    # P4：引用来源 [{index, doc_id, chunk_id, doc_title, snippet, ...}]
    # none_as_null=True：写 None 时落 SQL NULL 而不是 JSON 'null'，
    # 这样「未启用检索」(NULL) 与「检索了但无引用」([]) 在 SQL 层也能区分，
    # 便于后续统计/清洗（WHERE citations IS NULL 才有意义）。
    citations: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # 关联：所属会话
    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return (
            f"<Message id={self.id} conversation_id={self.conversation_id} "
            f"role={self.role!r}>"
        )


class Document(Base):
    """文档提交表（P1-B2 / P1-DB2）：用户上传的 Word / Markdown 原始文件元数据。

    说明：
      - 二进制不进库：仅存 original_path（对象存储相对 key）与元数据；原始文件落对象存储。
      - P1 阶段 user_id 可为空：此时尚未接入登录鉴权（P2-B2），前端不传 user_id；
        FK 在「非空时」才引用 users(id)，空值不触发引用约束（ON DELETE SET NULL）。
      - status 初值 'pending'（待审核）；审核/入库字段（reviewer_* / chunk_count 等）
        属 P3，此处一并建列，避免后续频繁改表。
    """

    __tablename__ = "documents"

    # 主键：PG 内置 gen_random_uuid() 生成
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 提交人；P1 可为空（无鉴权），用户被删时置 NULL（不级联删文档）
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 原始文件名（保留用户看到的名字；存储落盘名由后端用 uuid 生成，互不影响）
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # 对象存储相对路径（不含 bucket）：{scope}/{yyyy}/{mm}/{uuid}.{ext}
    original_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 文档类型：word（.doc/.docx）/ markdown（.md）；P5 扩展 image/audio/video
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # MIME 类型（可选，便于预览/下载时设置 Content-Type）
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    # 文件字节数（BIGINT，与 002 SQL 一致）
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    # 状态：pending(待审核) → approved/rejected(审核) → indexing/indexed(入库)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'pending'")
    )
    # ---- P3 审核字段 ----
    # 审核结论：pending(待审核) / approved(通过) / rejected(驳回)。
    # 与 status 的区别（见 sql/003_p3_db1_review.sql 字段职责划分）：
    #   status        整体生命周期，入库时会继续变为 indexing/indexed；
    #   review_status 审核结论，一旦落定不再随入库流程变化，供审核队列筛选与统计。
    #   两者在管理员审核动作中同步写入（见 review_service.review_document）。
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    # 审核人（管理员）
    reviewer_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 审核备注（驳回时必填理由）
    review_note: Mapped[Optional[str]] = mapped_column(Text)
    # 审核时间
    reviewed_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))
    # 入库后切片数（P3 向量化后回填）
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # ---- P3-DB2 入库字段 ----
    # 最近一次成功入库的时间；未入库 / 已下线时为 NULL
    indexed_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))
    # 最近一次入库失败原因；成功入库后置空（管理员据此排查「入库失败」）
    last_index_error: Mapped[Optional[str]] = mapped_column(Text)
    # P4：入库时使用的向量模型签名（模型名，或未配置 Key 时的 "mock"）。
    # 用于检测「向量已过期」：与当前配置不一致时提示重新入库，
    # 否则 mock 向量 / 旧模型向量会在检索时召回无关内容（见 sql/005）。
    embedding_model: Mapped[Optional[str]] = mapped_column(String(64))
    # ---- P5 多模态解析字段 ----
    # OCR / ASR 提取出的纯文本（图片=识别文字，音视频=转写文本）。
    # 为什么要存：① 管理员审核时可直接查看「解析出了什么」，不必重新跑一遍模型；
    #            ② 重入库失败时可对比新旧结果；③ 便于排查「入库了但检索不到」。
    # 文档类（word / markdown）为空——它们的正文由解析器直接从原文件读取。
    extracted_text: Mapped[Optional[str]] = mapped_column(Text)
    # 解析元信息：{kind, provider, model, chars, elapsed_ms, duration_seconds, segments, extractor}
    # none_as_null=True 的语义同 messages.citations：写 None 落 SQL NULL 而非 JSON null。
    extract_meta: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True))

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # 软删除时间戳，空=未删除（P3 清理/下线用）
    deleted_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Document id={self.id} filename={self.filename!r} status={self.status!r}>"


# ---------------------------------------------------------------------
# 表级 CHECK 约束：与 SQL 文件保持一致，保证 create_all 生成等价结构
# ---------------------------------------------------------------------
User.__table__.append_constraint(
    CheckConstraint(
        User.__table__.c.role.in_(["user", "admin"]),
        name="ck_users_role",
    )
)
Message.__table__.append_constraint(
    CheckConstraint(
        Message.__table__.c.role.in_(["user", "assistant", "system"]),
        name="ck_messages_role",
    )
)

# documents 表的取值约束：类型与状态集合，与 002 SQL 保持一致
Document.__table__.append_constraint(
    CheckConstraint(
        Document.__table__.c.file_type.in_(["word", "markdown", "image", "audio", "video"]),
        name="ck_documents_file_type",
    )
)
Document.__table__.append_constraint(
    CheckConstraint(
        Document.__table__.c.status.in_(
            ["pending", "approved", "rejected", "indexing", "indexed"]
        ),
        name="ck_documents_status",
    )
)
# P3-DB1：审核结论取值约束，与 003 SQL 的 ck_documents_review_status 保持一致
Document.__table__.append_constraint(
    CheckConstraint(
        Document.__table__.c.review_status.in_(["pending", "approved", "rejected"]),
        name="ck_documents_review_status",
    )
)


class DocumentChunk(Base):
    """文档切片向量表（P3-DB2：RAG 管线的向量存储）。

    对应 sql/004_p3_db2_vector.sql 的 document_chunks 表：
      - 一行 = 文档的一个切片 + 该切片的 embedding 向量；
      - doc_id 级联删除：文档被删时切片自动清理；
      - (doc_id, chunk_index) 唯一：重入库可用同一序号覆盖，避免重复堆积。

    写入方式（见 services/indexing_service.py）：
      解析 → 切片 → 批量 embedding → 批量 INSERT。
    """

    __tablename__ = "document_chunks"

    # 切片主键
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 所属文档；文档删除时级联清理切片
    doc_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 切片在文档内的序号（从 0 开始）
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 切片正文
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 切片估算 token 数（粗估，用于上下文预算与统计）
    token_count: Mapped[Optional[int]] = mapped_column(Integer)
    # 向量列：pgvector 的 vector(1024)。
    # pgvector 提供的 Vector 类型负责 Python list ↔ PG vector 的转换，
    # 写入时直接赋 list[float] 即可，查询时返回 list[float]。
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(EMBEDDING_DIM))
    # 切片元信息：{filename, file_type, heading}，P4 引用卡片展示用
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentChunk id={self.id} doc_id={self.doc_id} "
            f"chunk_index={self.chunk_index}>"
        )


# 与 004 SQL 的 uq_document_chunks_doc_index 对应，保证 create_all 生成等价结构
DocumentChunk.__table__.append_constraint(
    CheckConstraint(
        DocumentChunk.__table__.c.chunk_index >= 0,
        name="ck_document_chunks_index_nonnegative",
    )
)
# HNSW 向量索引（余弦距离）：P4 相似度召回的性能保障。
# 与 004 SQL 同名；create_all 与 SQL 脚本二者任一先建都不会重复创建（IF NOT EXISTS）。
Index(
    "idx_document_chunks_embedding",
    DocumentChunk.__table__.c.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
)


# =====================================================================
# P6 运营闭环：站内信（审核通知）+ 答案反馈
# ---------------------------------------------------------------------
# 对应 sql/007_p6_notifications.sql 与 sql/008_p6_feedback.sql。
# 两张表的取舍说明都写在各自的 SQL 文件头部，这里只补充字段与关系的映射。
# =====================================================================


class Notification(Base):
    """站内信 / 通知表（P6-DB1）：把「审核结果」主动推给提交人。

    业务背景（《需求清单》P6 功能1「审核通知」）：
        用户在 P1 提交文档后只能自己反复打开「我的文档」看状态，
        管理员审核完也不会通知到人。本表即「状态变更 → 通知」的落地存储，
        前端通过轮询未读数在顶栏铃铛上呈现，并对新通知弹出 Toast。

    为什么正文存快照（title/content）而不是渲染时查文档：
        文档可能被改名、被下线甚至被清理，历史通知不应随之变形——
        通知描述的是「当时发生了什么」，属于事实记录。
    """

    __tablename__ = "notifications"

    # 通知主键
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 收件人：通知必须有人可收；用户注销时其通知级联清理
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 通知类型：document_approved（通过）/ document_rejected（驳回）
    # 与 SQL 的 CHECK 约束一致（见下方 append_constraint）
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 标题：一句话结论（Toast 标题 / 列表主文案）
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 正文：含文件名、驳回理由等细节（列表展开可见）
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 关联文档：便于前端「点通知定位到我的文档」；文档被清理时置空，通知保留
    doc_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 已读时间：NULL=未读。用时间戳而非布尔值，可额外统计「多久才被查看」
    read_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Notification id={self.id} user_id={self.user_id} "
            f"type={self.type!r} read={self.read_at is not None}>"
        )


# 通知类型取值约束：与 007 SQL 的 CHECK 保持一致，保证 create_all 生成等价结构
Notification.__table__.append_constraint(
    CheckConstraint(
        Notification.__table__.c.type.in_(["document_approved", "document_rejected"]),
        name="ck_notifications_type",
    )
)
# 列表查询索引：收件人 + 时间倒序（与 007 SQL 同名，二者任一先建都不会重复）
Index(
    "idx_notifications_user_created",
    Notification.__table__.c.user_id,
    Notification.__table__.c.created_at.desc(),
)


class Feedback(Base):
    """答案反馈表（P6-DB2）：用户对某条助手回答的点赞 / 点踩。

    业务背景（《需求清单》P6 功能2「答案反馈与评估」）：
        「反馈采集用于后续 prompt/检索离线优化」——因此反馈必须挂在**具体回答**上，
        并带上点踩归因（reason），才能回答「哪类问题答不好」而不只是「有几个差评」。

    一条记录 = 一个用户对一条回答的一次评价：
        (message_id, user_id) 唯一，重复提交视为修改（先赞后踩很常见），
        不会堆出两条互相矛盾的记录。
    """

    __tablename__ = "feedback"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 被评价的消息：必须是助手回答（由 service 层校验 role='assistant'）
    message_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 评价人：用户注销后置空，反馈作为匿名评价保留（统计口径不因用户离开而失真）
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 评价结论：up / down
    rating: Mapped[str] = mapped_column(String(8), nullable=False)
    # 点踩归因：off_topic / wrong / unsupported / other（点赞时为空）
    reason: Mapped[Optional[str]] = mapped_column(String(32))
    # 用户补充说明（自由文本，可空；长度在 service 层校验）
    comment: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<Feedback id={self.id} message_id={self.message_id} "
            f"rating={self.rating!r}>"
        )


# 评价结论取值约束：与 008 SQL 的 ck 命名保持一致
Feedback.__table__.append_constraint(
    CheckConstraint(
        Feedback.__table__.c.rating.in_(["up", "down"]),
        name="ck_feedback_rating",
    )
)
# 点踩归因取值约束（允许 NULL：点赞时没有归因）
Feedback.__table__.append_constraint(
    CheckConstraint(
        Feedback.__table__.c.reason.is_(None)
        | Feedback.__table__.c.reason.in_(["off_topic", "wrong", "unsupported", "other"]),
        name="ck_feedback_reason",
    )
)
# 唯一约束：同一用户对同一条回答只有一条反馈
Index(
    "uq_feedback_message_user",
    Feedback.__table__.c.message_id,
    Feedback.__table__.c.user_id,
    unique=True,
)
# 管理员差评列表索引：按评价结论 + 时间倒序
Index(
    "idx_feedback_rating_created",
    Feedback.__table__.c.rating,
    Feedback.__table__.c.created_at.desc(),
)


# =====================================================================
# P7 生产加固：审计日志（合规）
# ---------------------------------------------------------------------
# 对应 sql/009_p7_audit_logs.sql。
#
# 设计要点（与 SQL 文件一一对应，改一处必须同步另一处）：
#   1. username / role 是**快照**——历史审计必须反映「当时是谁、以什么身份」，
#      不能 JOIN users 实时取值，否则用户改名/提权会改写历史事实。
#   2. user_id 外键为 ON DELETE SET NULL——账号注销后审计记录**不随之消失**，
#      这是合规（等保/审计留档）的硬要求。
#   3. resource_type + resource_id 用「类型 + 字符串 ID」而不是多个外键：
#      审计对象可能是文档、消息、备份文件、配置项，不可能为每种对象建列。
#   4. detail 用 JSONB 存动作细节，写入前由 service 层做敏感字段脱敏。
# =====================================================================


class AuditLog(Base):
    """审计日志表（P7-B2）：记录「谁、在什么时候、对什么、做了什么、结果如何」。

    与普通日志的分工：
      - 普通日志（core/logging.py）面向**排障**：请求路径、耗时、异常栈；
      - 审计日志面向**合规举证**：只记敏感动作，结构稳定、可长期保留、可检索。

    因此本表刻意不记录请求体/响应体，只记录动作与结果元信息。
    """

    __tablename__ = "audit_logs"

    # 主键：数据库侧生成，避免应用时钟不一致导致的排序问题
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 操作者 ID：账号注销后置空（审计记录保留）
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 操作者用户名快照（未登录的失败登录尝试为空）
    username: Mapped[Optional[str]] = mapped_column(String(64))
    # 操作者角色快照：user / admin / anonymous
    role: Mapped[Optional[str]] = mapped_column(String(16))
    # 动作代码：点号分层，如 auth.login / document.review / ops.backup.trigger
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    # 资源类型：document / message / user / backup / audit / config / llm
    resource_type: Mapped[Optional[str]] = mapped_column(String(32))
    # 资源标识（字符串以兼容 UUID 之外的资源，如备份文件名）
    resource_id: Mapped[Optional[str]] = mapped_column(String(128))
    # 结果：success / failure（与 SQL 的 CHECK 约束一致）
    result: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'success'")
    )
    # 来源信息：用于与访问日志 / Sentry 事件串联
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(512))
    request_id: Mapped[Optional[str]] = mapped_column(String(64))
    # 动作细节（已脱敏）：如 {"previous": "pending", "current": "approved"}
    detail: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True))
    # 发生时间：审计的核心排序与筛选字段
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} action={self.action!r} "
            f"user={self.username!r} result={self.result!r}>"
        )


# 结果取值约束：与 009 SQL 的 ck_audit_logs_result 一致，
# 保证 create_all 与 SQL 脚本生成同一结构
AuditLog.__table__.append_constraint(
    CheckConstraint(
        AuditLog.__table__.c.result.in_(["success", "failure"]),
        name="ck_audit_logs_result",
    )
)
# 列表主索引：管理端按时间倒序分页
Index("idx_audit_logs_created_at", AuditLog.__table__.c.created_at.desc())
# 按动作筛选：合规检查常问「审核动作最近做了哪些」
Index(
    "idx_audit_logs_action_created",
    AuditLog.__table__.c.action,
    AuditLog.__table__.c.created_at.desc(),
)
# 按用户追溯
Index(
    "idx_audit_logs_user_created",
    AuditLog.__table__.c.user_id,
    AuditLog.__table__.c.created_at.desc(),
)
# 按资源追溯
Index(
    "idx_audit_logs_resource",
    AuditLog.__table__.c.resource_type,
    AuditLog.__table__.c.resource_id,
)

