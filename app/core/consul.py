# app/core/consul.py
import consul
from typing import Optional, List
from .config import settings

class ConsulClient:
    def __init__(self):
        self.consul = consul.Consul(
            host=settings.CONSUL_HOST,
            port=settings.CONSUL_PORT
        )
    
    async def register_service(
        self, 
        service_name: str, 
        service_id: str, 
        address: str, 
        port: int, 
        tags: List[str]
    ):
        """注册服务到 Consul"""
        return self.consul.agent.service.register(
            name=service_name,
            service_id=service_id,
            address=address,
            port=port,
            tags=tags,
            check={
                "http": f"http://{address}:{port}/health",
                "interval": "10s",
                "timeout": "5s"
            }
        )
    
    async def get_service(self, service_name: str) -> Optional[str]:
        """获取服务地址"""
        _, services = self.consul.health.service(service_name, passing=True)
        
        
        print(services)

        if services:
            service = services[0]
            return f"http://{service['Service']['Address']}:{service['Service']['Port']}"
        return None

consul_client = ConsulClient()