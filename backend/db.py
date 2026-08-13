"""MongoDB connection (motor async driver)."""
from motor.motor_asyncio import AsyncIOMotorClient
from config import settings

# Fast-fail (5s) instead of hanging 30s if Atlas is unreachable (e.g. IP not allowlisted).
client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
db = client.get_default_database()
