import sys
import os
# 添加api_gateway目录到Python路径，确保app模块可以被导入
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))
# 添加项目根目录到Python路径，确保common模块可以被导入
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

import logging
import argparse
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.consul import consul_client
from app.core.health_check import health_checker
from app.core.middleware import GatewayMiddleware
from app.core.model_load_balencer import ModelLoadBalancer
from app.api import tools_api
from app.api import tools_base
from app.api import model_base
from app.api import kldge_base

logger = logging.getLogger(__name__)

# 全局大模型负载均衡器实例
model_lb = ModelLoadBalancer()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# 添加中间件
app.add_middleware(GatewayMiddleware)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 需要修改：根据实际需求配置允许的源
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# 注册路由
app.include_router(tools_api.router)  #工具转发api
app.include_router(kldge_base.router) #知识库metrics
app.include_router(model_base.router) #模型库metrics
app.include_router(tools_base.router) #工具库metrics,prefix="/toolsbase" 

@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "service": settings.SERVICE_NAME}

@app.get(f"{settings.API_V1_STR}/health")
async def api_health_check():
    """API健康检查端点"""
    return {"status": "healthy", "service": settings.SERVICE_NAME}

@app.on_event("startup")
async def startup_event():
    try:
        # 启动健康检查任务
        # await health_checker.start()
        await model_lb.initialize()
        logger.info(f"✅ {settings.APP_NAME}服务已启动")
    
    except Exception as e:
        logger.error(f"❌ 服务启动失败: {str(e)}")

@app.on_event("startup")
async def startup_event():
    """服务启动时注册到Consul"""
    try:
        # 获取实际运行端口
        port = int(os.getenv("SERVICE_PORT", settings.SERVICE_PORT))
        
        # 注册API网关服务到Consul
        await consul_client.register_service(
            service_name=settings.SERVICE_NAME,
            service_id=f"{settings.SERVICE_NAME}-{port}",
            address="localhost",
            #address="consul",
            port=port,
            tags=["api-gateway", "tools-library"]
        )
        print(f"✅ API网关服务已启动并注册到Consul，端口: {port}")
        
    except Exception as e:
        print(f"❌ API网关服务注册到Consul失败: {str(e)}")

@app.on_event("shutdown")
async def shutdown_event():
    """服务关闭时从Consul注销"""
    try:
        port = int(os.getenv("SERVICE_PORT", settings.SERVICE_PORT))
        consul_client.consul.agent.service.deregister(f"{settings.SERVICE_NAME}-{port}")
        print(f"✅ API网关服务已停止并从Consul注销")
    except Exception as e:
        print(f"❌ 从Consul注销服务失败: {str(e)}")

def get_port():
    """获取服务端口，优先级：命令行参数 > 环境变量 > 配置文件"""
    parser = argparse.ArgumentParser(description="API网关服务")
    parser.add_argument("--port", type=int, help="服务端口号")
    args, _ = parser.parse_known_args()
    
    if args.port:
        return args.port
    elif os.getenv("SERVICE_PORT"):
        return int(os.getenv("SERVICE_PORT"))
    else:
        return settings.SERVICE_PORT

if __name__ == "__main__":
    import uvicorn
    
    # 获取端口配置
    port = get_port()
    
    print(f"🚀 启动API网关服务，端口: {port}")
    print(f"📖 API文档: http://localhost:{port}/docs")
    print(f"🔍 健康检查: http://localhost:{port}/health")
    print(f"🌐 网关路由: http://localhost:{port}/tools/api/v1/{{service}}/{{path}}")
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=True
    )
