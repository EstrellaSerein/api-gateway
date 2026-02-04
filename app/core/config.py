# app/core/config.py
from common.config.settings import BaseAppSettings
import os
from typing import List, Dict, Any, Optional
import json
from pydantic import validator, Field
import logging

logger = logging.getLogger(__name__)

class Settings(BaseAppSettings):
    """API 网关服务配置"""
    
    # 服务基本配置
    SERVICE_NAME: str = os.getenv("API_GATEWAY_SERVICE_NAME", "api-gateway")
    SERVICE_PORT: int = int(os.getenv("API_GATEWAY_SERVICE_PORT", "8012"))
    APP_ADDRESS: str = os.getenv("API_GATEWAY_APP_ADDRESS", "api-gateway")
    APP_NAME: str = os.getenv("API_GATEWAY_APP_NAME", "API 网关服务")
    APP_DESCRIPTION: str = os.getenv("API_GATEWAY_APP_DESCRIPTION", "API 网关服务")
    APP_VERSION: str = os.getenv("API_GATEWAY_APP_VERSION", "1.0.0")
    GATWEY_HEALTH_CHECK_INTERVAL: int = int(os.getenv("GATWEY_HEALTH_CHECK_INTERVAL", "1000"))
    GATWEY_HEALTH_CHECK_TIMEOUT: int = int(os.getenv("GATWEY_HEALTH_CHECK_TIMEOUT", "10"))
    REQUEST_TIMEOUT:  int = int(os.getenv("GATWEY_HEALTH_REQUEST_TIMEOUT", "120"))
    RATE_LIMIT_PER_MINUTE:  int = int(os.getenv("GATWEY_HEALTH_RATE_LIMIT_PER_MINUTE", "120"))
    # 模型实例配置
    MODEL_INSTANCES: Dict[str, List[Dict[str, Any]]] = Field(
        default_factory=dict,
        description="大模型实例配置"
    )
    
    # 服务健康检查配置
    @property
    def SERVICE_HEALTH_CHECKS(self) -> List[Dict[str, Any]]:
        """服务健康检查配置"""
        checks_json = os.getenv("SERVICE_HEALTH_CHECKS", '''[
            {
                "name": "service_nlp2sql",
                "ch_name": "自然语言转SQL服务",
                "health_check_url": "http://service_nlp2sql:8004/health",
                "qps_threshold": 110,
                "response_time_threshold": 480.0
            }
        ]''')
        try:
            return json.loads(checks_json)
        except json.JSONDecodeError as e:
            logger.error(f"解析SERVICE_HEALTH_CHECKS失败: {str(e)}")
            return []
    
    # 标签配置
    @property
    def APP_TAG(self) -> List[str]:
        """应用标签"""
        tags = os.getenv("API_GATEWAY_APP_TAG", "api_gateway,tools_library")
        return [tag.strip() for tag in tags.split(",")]
    
    # 验证器 - 解析模型实例配置
    @validator("MODEL_INSTANCES", pre=True)
    def parse_model_instances(cls, value):
        """解析模型实例配置"""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as e:
                logger.error(f"解析MODEL_INSTANCES失败: {str(e)}")
                return {}
        return value or {}
    
    # 初始化方法 - 从环境变量加载模型实例配置
    def __init__(self, **data):
        super().__init__(**data)
        # 从环境变量加载模型实例配置
        model_instances_json = os.getenv("MODEL_INSTANCES_JSON")
        if model_instances_json:
            try:
                self.MODEL_INSTANCES = json.loads(model_instances_json)
                logger.info(f"从环境变量加载模型实例配置: {len(self.MODEL_INSTANCES)} 个模型")
            except json.JSONDecodeError as e:
                logger.error(f"解析环境变量MODEL_INSTANCES_JSON失败: {str(e)}")
                self.MODEL_INSTANCES = {}

settings = Settings()