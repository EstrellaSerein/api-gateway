from fastapi import APIRouter, HTTPException, Request, Security
from app.core.security import verify_api_key
from app.core.consul import consul_client
from app.core.config import settings
import httpx
import time
from typing import Dict, Any
from app.core.monitoring import monitor
from app.core.model_load_balencer import ModelLoadBalancer

from urllib.parse import urlparse
import logging

# 设置日志
logger = logging.getLogger("api_gateway")

router = APIRouter()

def get_service_config(service_name: str) -> Dict[str, Any]:
    """
    从配置中获取服务配置
    
    参数:
    service_name: 服务名称
    
    返回:
    服务配置字典 (包含 protocol, host, port)
    """
    # 遍历配置的服务列表
    for service in settings.SERVICE_HEALTH_CHECKS:
        if service["name"].lower() == service_name.lower():
            # 解析健康检查URL获取主机和端口
            parsed = urlparse(service["health_check_url"])
            port = parsed.port
            if not port:
                # 根据协议设置默认端口
                port = 443 if parsed.scheme == "https" else 80
            return {
                "protocol": parsed.scheme,
                "host": parsed.hostname,
                "port": port
            }
    
    # 如果找不到服务，返回默认值
    return {
        "protocol": "http",
        "host": service_name,
        "port": 8000
    }

async def forward_to_service(service: str, path: str, request: Request) -> Dict[str, Any]:
    """
    转发请求到目标服务
    
    参数:
    service: 服务名称
    path: 请求路径
    request: 原始请求对象
    
    返回:
    目标服务的响应内容
    """
    # 获取服务配置
    service_config = get_service_config(service)
    
    async with httpx.AsyncClient() as client:
        # 构建目标 URL
        target_url = f"{service_config['protocol']}://{service_config['host']}:{service_config['port']}/{path}"
        logger.info(f"转发请求到服务: {service} -> {target_url}")
        
        # 获取原始请求的查询参数
        params = dict(request.query_params)
        
        # 获取原始请求的 headers
        headers = dict(request.headers)
        headers.pop("host", None)  # 移除 host header
        
        # 获取原始请求的 body
        body = await request.body()
        
        # 记录请求开始
        await monitor.record_request_start(service)
        
        start_time = time.time()
        try:
            # 发送请求
            response = await client.request(
                method=request.method,
                url=target_url,
                params=params,
                headers=headers,
                content=body,
                timeout=settings.REQUEST_TIMEOUT
            )
            
            response_time = time.time() - start_time
            
            # 记录请求成功
            await monitor.record_request_end(
                service, 
                response_time,
                is_error=(response.status_code >= 400)
            )
            
            # 直接返回目标服务的响应
            try:
                # 尝试解析为JSON对象
                return response.json()
            except ValueError:
                # 非JSON响应则保持原始数据
                return {"content": response.text}
            
        except httpx.TimeoutException:
            response_time = time.time() - start_time
            await monitor.record_request_end(service, response_time, is_error=True)
            raise HTTPException(
                status_code=504,
                detail="Gateway Timeout"
            )
        except httpx.RequestError as e:
            response_time = time.time() - start_time
            await monitor.record_request_end(service, response_time, is_error=True)
            raise HTTPException(
                status_code=502,
                detail=f"Bad Gateway: {str(e)}"
            )

@router.api_route("/tools/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def forward_request(
    service: str, 
    path: str, 
    request: Request,
    #api_key: str = Security(verify_api_key)
):
    """固定服务路由"""
    logger.info(f"收到请求: 服务={service}, 路径={path}")
    
    # 检查服务是否在配置中
    service_names = [s["name"].lower() for s in settings.SERVICE_HEALTH_CHECKS]
    if service.lower() not in service_names:
        logger.warning(f"服务未配置: {service}")
        raise HTTPException(
            status_code=404,
            detail=f"服务 {service} 未在配置中定义"
        )
        
    # 转发请求
    return await forward_to_service(service, path, request)

# 健康检查端点
@router.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy"}