# app/core/middleware.py
import uuid
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from common.utils.logger import setup_logger

# 创建logger实例
logger = setup_logger("api_gateway", "logs/api_gateway.log")

class GatewayMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        
        logger.info(f"Request started: {request_id} - {request.method} {request.url}")
        
        try:
            response = await call_next(request)
            return response
        except Exception as e:
            logger.error(f"Request failed: {request_id}, error: {str(e)}")
            raise