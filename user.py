import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey,
    Enum as SAEnum, Text, Integer
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base
import enum


class UserRole(str, enum.Enum):
    STAFF = "staff"
    LINE_MANAGER = "lm"
    HR_ADMIN = "hr_admin"
    SYSTEM_ADMIN = "system_admin"


class GroupType(str, enum.Enum):
    OWNER = "owner"
    COLLABORATOR = "collaborator"
    MEMBER = "member"
    BACKUP = "backup"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=True)   # null = SSO-only users
    role = Column(SAEnum(UserRole), nullable=False, default=UserRole.STAFF)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    org = relationship("Organisation", back_populates="users")
    memberships = relationship("GroupMembership", back_populates="user", cascade="all, delete-orphan")
    staff_assessments = relationship("StaffAssessment", back_populates="user", foreign_keys="StaffAssessment.user_id")
    created_assessments = relationship("Assessment", back_populates="created_by_user", foreign_keys="Assessment.created_by")


class Organisation(Base):
    __tablename__ = "organisations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="org")
    departments = relationship("Department", back_populates="org")
    knowledge_sources = relationship("KnowledgeSource", back_populates="org")
    security_groups = relationship("SecurityGroup", back_populates="org")


class Department(Base):
    __tablename__ = "departments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    org = relationship("Organisation", back_populates="departments")
    user_departments = relationship("UserDepartment", back_populates="department")


class UserDepartment(Base):
    """Many-to-many: a user can belong to multiple departments."""
    __tablename__ = "user_departments"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id"), primary_key=True)
    title = Column(String(255))           # job title within that department
    line_manager_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    department = relationship("Department", back_populates="user_departments")
    line_manager = relationship("User", foreign_keys=[line_manager_id])


class SecurityGroup(Base):
    __tablename__ = "security_groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    name = Column(String(100), nullable=False)          # e.g. owner.data-analytics
    group_type = Column(SAEnum(GroupType), nullable=False)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    org = relationship("Organisation", back_populates="security_groups")
    memberships = relationship("GroupMembership", back_populates="group", cascade="all, delete-orphan")


class GroupMembership(Base):
    __tablename__ = "group_memberships"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    group_id = Column(UUID(as_uuid=True), ForeignKey("security_groups.id"), primary_key=True)
    is_owner = Column(Boolean, default=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="memberships")
    group = relationship("SecurityGroup", back_populates="memberships")
