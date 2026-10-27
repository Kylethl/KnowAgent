# ORM 模型（P1-DB1 / P1-DB2）：users / conversations / messages / documents
# ---------------------------------------------------------------------
# 与 backend/sql/001_p1_db1_schema.sql、002_p1_db2_documents.sql 字段一一对应；
# 该 SQL 文件为「建表规范源头」，本文件仅用于应用层读写。
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
    # P4：引用来源 [{doc_id, chunk, snippet}]
    citations: Mapped[Optional[dict]] = mapped_column(JSONB)
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
