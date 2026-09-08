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
from app.models.mcp import (
    MCPServer,
    MCPServerCheck,
    MCPServerDiscovery,
    MCPTool,
    MCPToolPolicy,
    MCPToolVerification,
    MCPToolVersion,
)
from app.models.model_profile import EmbeddingProfile, LLMProfile

__all__ = [
    "Agent",
    "AgentToolGrant",
    "AgentVersion",
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
    "User",
    "UserRole",
]
