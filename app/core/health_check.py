import asyncio
import time
import httpx
import logging
from typing import Dict, Union, List, Optional
from collections import defaultdict
from app.core.config import settings  # 导入配置

# 设置日志
logger = logging.getLogger("health_check")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

class HealthChecker:
    """
    健康检查器类 - 负责服务的主动健康检查
    
    功能:
    1. 定期检查所有配置服务的健康状态
    2. 维护服务的健康状态信息
    3. 提供健康状态查询接口
    """
    
    def __init__(self):
        """初始化健康检查器"""
        # 从配置加载服务列表
        self.services = settings.SERVICE_HEALTH_CHECKS
        
        # 初始化服务指标
        self.service_metrics = self.initialize_service_metrics()
        
        self.lock = asyncio.Lock()
        self.health_check_task = None
        self.interval = settings.GATWEY_HEALTH_CHECK_INTERVAL  
        self.timeout = settings.GATWEY_HEALTH_CHECK_TIMEOUT
        
    def initialize_service_metrics(self) -> Dict[str, Dict]:
        """根据配置初始化服务指标"""
        metrics = {}
        for service in self.services:
            service_name = service["name"]
            metrics[service_name] = {
                "last_health_check": time.time(),
                "healthy": True,
                "health_check_failures": 0,
                "last_healthy_check": time.time(),
                "health_check_url": service["health_check_url"],
                "qps_threshold": service.get("qps_threshold", 100),
                "response_time_threshold": service.get("response_time_threshold", 500.0)
            }
            logger.info(f"✅ 初始化服务监控: {service_name}")
        return metrics
    
    async def start(self):
        """启动健康检查后台任务"""
       
        logger.info(f"🚀 启动健康检查后台任务，间隔: {self.interval}秒")
        
        # 如果任务已经在运行，先取消
        if self.health_check_task and not self.health_check_task.done():
            self.health_check_task.cancel()
            try:
                await self.health_check_task
            except asyncio.CancelledError:
                pass
        
        # 创建新任务
        self.health_check_task = asyncio.create_task(self._run_health_checks())
    
    async def stop(self):
        """停止健康检查任务"""
        if self.health_check_task and not self.health_check_task.done():
            self.health_check_task.cancel()
            try:
                await self.health_check_task
            except asyncio.CancelledError:
                logger.info("健康检查任务已停止")
    
    async def _run_health_checks(self):
        """健康检查任务主循环"""
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    await self.perform_health_checks()
                    logger.debug(f"✅ 已完成健康检查轮询")
                except Exception as e:
                    logger.error(f"❌ 健康检查任务出错: {str(e)}")
        except asyncio.CancelledError:
            logger.info("健康检查任务被取消")
        except Exception as e:
            logger.error(f"健康检查任务意外终止: {str(e)}")
    
    async def perform_health_checks(self):
        """对所有配置服务执行健康检查"""
        current_time = time.time()
        
        async with self.lock:
            # 获取所有服务列表
            logger.info("开始执行健康检查")
            
            # 使用配置的服务列表
            services = [service["name"] for service in self.services]
            if not services:
                logger.warning("⚠️ 配置文件中没有定义服务")
                return
                
            logger.info(f"开始健康检查: {len(services)}个服务")
            
            # 使用httpx异步客户端
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
                tasks = [
                    self.check_service_health(client, service_name, self.service_metrics[service_name]["health_check_url"])
                    for service_name in services
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 更新健康状态
            for service_name, result in zip(services, results):
                metrics = self.service_metrics[service_name]
                
                # 更新最后健康检查时间
                metrics["last_health_check"] = current_time
                
                if isinstance(result, Exception):
                    # 健康检查失败
                    metrics["health_check_failures"] += 1
                    logger.warning(f"健康检查失败[{service_name}]: {str(result)}")
                    
                    # 失败阈值 (连续3次失败视为不健康)
                    if metrics["health_check_failures"] >= 3:
                        metrics["healthy"] = False
                else:
                    # 成功获取结果
                    metrics["health_check_failures"] = 0  # 重置失败计数
                    metrics["healthy"] = result  # result是布尔值
                    
                    if not result:
                        logger.warning(f"服务不健康[{service_name}]: 健康检查返回失败状态")
    
    async def check_service_health(self, client: httpx.AsyncClient, service_name: str, url: str) -> Union[bool, Exception]:
        """
        检查单个服务的健康状态
        
        参数:
        client: httpx异步客户端
        service_name: 服务名称
        url: 健康检查URL
        
        返回:
        True: 服务健康
        False: 服务不健康
        Exception: 健康检查失败
        """
        try:
            logger.debug(f"检查服务健康: {service_name} -> {url}")
            response = await client.get(url)
            
            # 检查HTTP状态码
            if response.status_code != 200:
                logger.warning(f"服务健康检查失败[{service_name}]: HTTP状态码 {response.status_code}")
                return False
                
            try:
                # 尝试解析JSON响应
                data = response.json()
                
                # 支持多种健康响应格式
                if "status" in data and data["status"] == "healthy":
                    return True
                elif "healthy" in data and data["healthy"] is True:
                    return True
                elif "status" in data and data["status"] == "unhealthy":
                    return False
                elif "healthy" in data and data["healthy"] is False:
                    return False
                
                # 默认仅根据状态码判断
                return True
            except ValueError:
                # 非JSON响应则仅根据状态码判断
                return True
        except httpx.TimeoutException:
            logger.warning(f"服务健康检查超时[{service_name}]: {self.timeout}秒内无响应")
            return TimeoutError(f"检查服务 {service_name} 超时")
        except httpx.NetworkError:
            logger.warning(f"服务健康检查网络错误[{service_name}]: 无法连接")
            return ConnectionError(f"无法连接到服务 {service_name}")
        except Exception as e:
            logger.error(f"服务健康检查异常[{service_name}]: {str(e)}")
            return e
    
    def get_service_health(self, service_name: str) -> bool:
        """获取服务的健康状态"""
        if service_name not in self.service_metrics:
            return False
            
        return self.service_metrics[service_name]["healthy"]
    
    
    
    def get_service_thresholds(self, service_name: str) -> Dict[str, float]:
        """获取服务的阈值配置"""
        if service_name not in self.service_metrics:
            return {
                "qps_threshold": 100,
                "response_time_threshold": 500.0
            }
            
        metrics = self.service_metrics[service_name]
        return {
            "qps_threshold": metrics["qps_threshold"],
            "response_time_threshold": metrics["response_time_threshold"]
        }
    
    def get_all_health_status(self) -> Dict[str, dict]:
        """获取所有服务的健康状态"""
        health_status = {}
        
        for service_name, metrics in self.service_metrics.items():
            health_status[service_name] = {
                "healthy": metrics["healthy"],
                "last_check": time.time() - metrics["last_health_check"],
                "failures": metrics["health_check_failures"],
                "qps_threshold": metrics["qps_threshold"],
                "response_time_threshold": metrics["response_time_threshold"]
            }
        
        return health_status
    
    def reload_config(self):
        """重新加载配置文件"""
        logger.info("♻️ 重新加载服务配置")
        self.services = settings.SERVICE_HEALTH_CHECKS
        self.service_metrics = self.initialize_service_metrics()
        logger.info(f"✅ 服务配置已重新加载，服务数量: {len(self.service_metrics)}")

# 全局健康检查器实例
health_checker = HealthChecker()