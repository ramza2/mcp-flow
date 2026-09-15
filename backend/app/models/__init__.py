"""ORM model package — import models so metadata is registered for Alembic."""

from app.models.agent import Agent, AgentToolGrant, AgentVersion
from app.models.approval import ApprovalPolicy
from app.models.auth import (
    Permission,
    ResourceGrant,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.models.conversation import AgentRequest, Conversation, ConversationMessage
from app.models.execution import Execution, ExecutionStep
from app.models.idempotency import ApiIdempotencyRecord
from app.models.mcp import (
    MCPServer,
    MCPServerCheck,
    MCPServerDiscovery,
    MCPTool,
    MCPToolPolicy,
    MCPToolVerification,
    MCPToolVersion,
    ToolEmbedding,
)
from app.models.model_profile import EmbeddingProfile, LLMProfile
from app.models.parameter_build import ParameterBuildRun
from app.models.plan_generation import PlanGenerationRun, PlanGenerationToolRef
from app.models.plan_validation import PlanValidationRun
from app.models.session import Session
from app.models.tool_selection import (
    ClarificationRequest,
    ToolSelectionCandidate,
    ToolSelectionRun,
)

__all__ = [
    "Agent",
    "AgentRequest",
    "AgentToolGrant",
    "AgentVersion",
    "ClarificationRequest",
    "ApiIdempotencyRecord",
    "Conversation",
    "ConversationMessage",
    "ApprovalPolicy",
    "Execution",
    "ExecutionStep",
    "EmbeddingProfile",
    "LLMProfile",
    "MCPServer",
    "MCPServerCheck",
    "MCPServerDiscovery",
    "MCPTool",
    "MCPToolPolicy",
    "MCPToolVerification",
    "MCPToolVersion",
    "ParameterBuildRun",
    "Permission",
    "PlanGenerationRun",
    "PlanGenerationToolRef",
    "PlanValidationRun",
    "ResourceGrant",
    "Role",
    "RolePermission",
    "Session",
    "ToolEmbedding",
    "ToolSelectionCandidate",
    "ToolSelectionRun",
    "User",
    "UserRole",
]
