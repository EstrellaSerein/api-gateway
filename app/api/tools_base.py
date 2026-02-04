from fastapi import APIRouter
import time
from app.core.monitoring import (
    monitor, 
    MetricsResponse
)
router = APIRouter()

@router.get("/toolsbase/metrics", response_model=MetricsResponse)
async def get_metrics():
    """获取网关和服务监控指标"""
    # 直接调用监控器的get_metrics方法
    return await  monitor.get_metrics()
