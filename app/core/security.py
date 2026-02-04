from fastapi import Security, HTTPException
from fastapi.security import APIKeyHeader
from .config import settings

api_key_header = APIKeyHeader(name=settings.API_KEY_HEADER)

async def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key != settings.API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key"
        )
    return api_key
