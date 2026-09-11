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
from app.models.session import Session

__all__ = [
    "Agent",
    "AgentRequest",
    "AgentToolGrant",
    "AgentVersion",
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
    "Permission",
    "ResourceGrant",
    "Role",
    "RolePermission",
    "Session",
    "ToolEmbedding",
    "User",
    "UserRole",
]
