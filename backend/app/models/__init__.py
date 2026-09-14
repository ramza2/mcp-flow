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
    "Conversation",
    "ConversationMessage",
    "ApprovalPolicy",
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
