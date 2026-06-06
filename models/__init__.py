"""
Models package — all ORM models consolidated for easy importing.
"""

from models.user import (
    User, Organisation, Department, UserDepartment,
    SecurityGroup, GroupMembership,
    UserRole, GroupType,
)

from models.assessment import (
    Assessment, AssessmentTarget, Question, StaffAssessment, StaffAnswer,
    AssessmentType, QuestionType, AssessmentStatus, InformationSource,
    TargetType, StaffAssessmentStatus,
)

from models.knowledge import (
    KnowledgeSource, DocumentChunk, AuditLog,
    SourceType, SourceStatus,
)

from models.system import PipelineRun, PipelineStep, PipelineSpan, RagSample

__all__ = [
    # User models
    "User", "Organisation", "Department", "UserDepartment",
    "SecurityGroup", "GroupMembership", "UserRole", "GroupType",
    # Assessment models
    "Assessment", "AssessmentTarget", "Question", "StaffAssessment", "StaffAnswer",
    "AssessmentType", "QuestionType", "AssessmentStatus", "InformationSource",
    "TargetType", "StaffAssessmentStatus",
    # Knowledge models
    "KnowledgeSource", "DocumentChunk", "AuditLog", "SourceType", "SourceStatus",
    # System / observability
    "PipelineRun", "PipelineStep", "PipelineSpan", "RagSample",
]
