"""
API package — re-exports routers from the root-level router modules.
"""
import sys
import os

# Ensure the project root is on the path so the router files can be found
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import router as auth_router
from assessments import router as assessments_router
from knowledge import router as knowledge_router
from admin import router as admin_router
from ops import router as ops_router
from users import router as users_router
from groups import router as groups_router

__all__ = ["auth_router", "assessments_router", "knowledge_router", "admin_router", "ops_router", "users_router", "groups_router"]
