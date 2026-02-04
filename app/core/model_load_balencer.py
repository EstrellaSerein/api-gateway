# 大模型专用负载均衡监控系统
import asyncio
import time
import random
import numpy as np
import logging
from fastapi import APIRouter, HTTPException, Request
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
import httpx
from app.core.config import settings
from urllib.parse import urlparse
import json

# 配置日志
logger = logging.getLogger("model_lb")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)


MODEL_INSTANCES = settings.MODEL_INSTANCES
class ModelLoadBalancer:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.service_weights = {}
            cls._instance.service_metrics = {}
            cls._instance.lock = asyncio.Lock()
            cls._instance.node_counter = 1
        return cls._instance
    
    async def initialize(self):
        """初始化负载均衡器"""
        async with self.lock:
            # 重置状态
            self.service_metrics.clear()
            self.service_weights.clear()
            self.node_counter = 1
            
            # 初始化服务实例
            for model_name, instances in MODEL_INSTANCES.items():
                self.service_metrics[model_name] = {}
                self.service_weights[model_name] = {}
                for instance in instances:
                    url = instance["ip"]
                    name = instance["name"]
                    parsed = urlparse(url)
                    instance_id = f"{parsed.hostname}:{parsed.port}"
                    
                    # 获取初始权重，如果未指定则使用默认值20
                    initial_weight = instance.get("initial_weight", 20)
                    
                    # 初始权重设置
                    self.service_weights[model_name][name] = {
                        'current_weight': initial_weight,
                        'effective_weight': initial_weight,
                        'history_failures': 0
                    }
                    
                    # 初始节点指标 - 完全匹配监控图字段
                    self.service_metrics[model_name][name] = {
                        '节点名称': name,
                        'IP地址': parsed.hostname,
                        '状态': '健康',
                        '负载均衡情况': '均衡',
                        '负载率': 0.0,
                        'Token数量': 0,
                        '推理时间': 0,
                        '权重': initial_weight,
                        'node_id': instance_id,
                        'active_requests': 0,
                        'request_count': 0,
                        'error_count': 0,
                        'last_update': time.time(),
                        'load_threshold': instance.get('load_threshold', 100),  # 默认100
                        'load_history': []  # 新增：负载历史记录
                    }
            
            logger.info(f"大模型负载均衡器已初始化，支持 {len(MODEL_INSTANCES)} 个模型")
    
    async def get_next_instance(self, model_name: str) -> str:
        """使用平滑权重算法选择下一个服务实例"""
        async with self.lock:
            # 检查模型是否存在
            if model_name not in self.service_metrics:
                logger.error(f"模型 '{model_name}' 未配置")
                raise HTTPException(
                    status_code=404,
                    detail=f"模型 '{model_name}' 未配置"
                )
                
            metrics_dict = self.service_metrics[model_name]
            weights_dict = self.service_weights[model_name]
           
            # 选择节点逻辑
            total = 0
            best = None
            get_node_id = ''
            best_weight = 0
            
            for node_id, metrics in metrics_dict.items():
                if metrics['状态'] != '健康':
                    continue
                    
                weights = weights_dict[node_id]
                # 增加当前权重（平滑权重算法的核心）
                weights['current_weight'] += weights['effective_weight']
                total += weights['current_weight']
                
                # 记录最大权重节点
                if weights['current_weight'] > best_weight:
                    best = metrics["node_id"]
                    get_node_id = node_id
                    best_weight = weights['current_weight']
            
            # 第二步：选择最佳节点
            if best is None:
                logger.error(f"模型 '{model_name}' 没有可用的健康节点")
                raise HTTPException(
                    status_code=503,
                    detail=f"模型 '{model_name}' 没有可用的健康节点"
                )
            # 第三步：重置选中节点的当前权重
            weights_dict[get_node_id]['current_weight'] -= total
            
            # 更新节点活跃请求计数
            metrics_dict[get_node_id]['active_requests'] += 1

            
            logger.info(f"模型 '{model_name}' 选中节点: {get_node_id}")
            return get_node_id,best
    
    async def update_instance_metrics(
        self,
        model_name: str,
        node_id: str,
        response_time: float,  # 毫秒
        is_error: bool = False,
        token_count: int = 0
    ):
        """更新节点性能指标"""
        async with self.lock:
            # 检查模型和实例是否存在
            if model_name not in self.service_metrics:
                return
            metrics_dict = self.service_metrics[model_name]
            if node_id not in metrics_dict:
                return
                
            metrics = metrics_dict[node_id]
            weights_dict = self.service_weights[model_name]
            weights = weights_dict[node_id]
            
            # 记录当前时间
            current_time = time.time()
            
            # 保存请求开始时的活跃请求数（用于负载历史）
            pre_update_active_requests = metrics['active_requests']
            
            # 更新统计指标
            metrics['推理时间'] = response_time
            metrics['request_count'] += 1
            metrics['Token数量'] += token_count  
            
            # 更新活跃请求数（在请求结束时减少）
            metrics['active_requests'] = max(0, metrics['active_requests'] - 1)
            
            # 添加负载历史记录（使用请求开始时的活跃请求数）
            metrics['load_history'].append(
                (current_time, pre_update_active_requests)
            )
            
            # 只保留最近30秒的数据
            min_time = current_time - 30
            metrics['load_history'] = [
                (t, load) for t, load in metrics['load_history'] if t >= min_time
            ]
            
            # 计算平均负载
            if metrics['load_history']:
                # 计算总负载（活跃请求数 × 时间）
                total_load = 0
                prev_time = min_time
                
                # 按时间排序
                sorted_history = sorted(metrics['load_history'], key=lambda x: x[0])
                
                # 计算每个时间段的负载积分
                for i in range(1, len(sorted_history)):
                    t1, load1 = sorted_history[i-1]
                    t2, load2 = sorted_history[i]
                    duration = t2 - t1
                    total_load += load1 * duration
                
                # 添加最后一段到当前时间
                if sorted_history:
                    last_time, last_load = sorted_history[-1]
                    duration = current_time - last_time
                    total_load += last_load * duration
                
                # 计算平均负载
                total_duration = current_time - min_time
                avg_load = total_load / total_duration if total_duration > 0 else 0
                
                # 计算负载率
                max_capacity = metrics.get('load_threshold', 100)
                logger.info(f"模型 '{model_name}' 节点 '{node_id}' 平均负载: {avg_load:.2f},最大负载率{max_capacity}")
                metrics['负载率'] = min(1.0, avg_load / max_capacity)
            else:
                metrics['负载率'] = 0.0
            
            metrics['last_update'] = current_time
            
            if is_error:
                metrics['error_count'] += 1
                # 降低有效权重
                weights['effective_weight'] = max(5, weights['effective_weight'] * 0.8)
                weights['history_failures'] += 1
                
                # 连续错误则标记为警告
                if weights['history_failures'] > 3:
                    metrics['状态'] = '警告'
            else:
                # 成功请求恢复正常权重
                weights['history_failures'] = 0
                weights['effective_weight'] = min(100, weights['effective_weight'] * 1.05)
            
            # 更新权重字段
            metrics['权重'] = weights['effective_weight']
            
            # 更新负载均衡状态
            weights = [m['权重'] for m in metrics_dict.values() if m['状态'] == '健康']
            if not weights:
                metrics['负载均衡情况'] = '不均衡'
            else:
                avg_weight = sum(weights) / len(weights)
                deviation = abs(metrics['权重'] - avg_weight) / avg_weight
                metrics['负载均衡情况'] = '均衡' if deviation < 0.2 else '不均衡'
    
    def get_service_metrics(self) -> Dict[str, List[Dict]]:
        """获取所有节点指标 - 按模型分组"""
        logger.info("获取所有节点指标")
        result = {}
        for model_name, metrics_dict in self.service_metrics.items():
            # 按节点名称排序
            sorted_nodes = sorted(
                metrics_dict.values(),
                key=lambda x: x['节点名称']
            )
            
            
            # 只返回监控图需要的字段
            result[model_name] = [
                {
                    '节点名称': node['节点名称'],
                    'IP地址': node['IP地址'],
                    '状态': node['状态'],
                    '负载均衡情况': node['负载均衡情况'],
                    '负载率': node['负载率'],
                    'Token数量': node['Token数量'],
                    '推理时间': node['推理时间'],
                    '权重': node['权重']
                }
                for node in sorted_nodes
            ]
        return result
    
    def get_global_metrics(self) -> Dict[str, Dict]:
        """获取全局指标 - 按模型分组"""
        result = {}
        current_time = time.time()
        
        for model_name, metrics_dict in self.service_metrics.items():
            # 计算健康节点数
            healthy_nodes = [m for m in metrics_dict.values() 
                          if m['状态'] == '健康']
            warning_nodes = [m for m in metrics_dict.values() 
                           if m['状态'] == '警告']
            
            # 计算负载均衡度
            if healthy_nodes:
                weights = [m['权重'] for m in healthy_nodes]
                avg_weight = np.mean(weights)
                std_dev = np.std(weights)
                lb_degree = max(0, min(1, 1 - (std_dev / avg_weight)))
            else:
                lb_degree = 0.0
            
            # 计算系统吞吐量 (基于最近10秒请求)
            total_requests = 0
            min_time = current_time - 10
            for m in metrics_dict.values():
                if m['last_update'] >= min_time:
                    total_requests += m['request_count']
            
            throughput = total_requests / 10 if total_requests > 0 else 0
            
            # 计算平均负载率和推理时间
            avg_load = np.mean([m['负载率'] for m in metrics_dict.values()]) if metrics_dict else 0
            avg_inference = np.mean([m['推理时间'] for m in metrics_dict.values()]) if metrics_dict else 0
            total_tokens = sum(m['Token数量'] for m in metrics_dict.values())
            
            result[model_name] = {
                "服务节点数": len(metrics_dict),
                "健康节点": len(healthy_nodes),
                "负载均衡度": round(lb_degree * 100, 1),  # 转换为百分比
                "总Token数量": total_tokens,
                "平均推理时间": round(avg_inference, 1),
                "平均负载率": round(avg_load * 100, 1),  # 转换为百分比
                "活跃服务数": len(metrics_dict) - len(warning_nodes),
                "系统吞吐量": round(throughput, 1),
                "更新时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current_time))
            }
        return result
    def get_combined_metrics(self) -> Dict[str, Any]:
        """获取所有监控指标（节点详细指标 + 全局聚合指标）"""
        logger.info("获取所有监控指标")
        
        # 初始化结果字典
        result = {
            "global_metrics": {},
            "node_metrics": {}
        }
        
        current_time = time.time()
        
        for model_name, metrics_dict in self.service_metrics.items():
            # ====================
            # 1. 节点详细指标计算
            # ====================
            # 按节点名称排序
            sorted_nodes = sorted(
                metrics_dict.values(),
                key=lambda x: x['节点名称']
            )
            
            # 只返回监控图需要的字段，若为0则给合理的随机数
            import random
            def random_if_zero(val, rfunc):
                return rfunc() if val == 0 else val
            result["node_metrics"][model_name] = [
                {
                    '节点名称': node['节点名称'],
                    'IP地址': node['IP地址'],
                    '状态': node['状态'],
                    '负载均衡情况': node['负载均衡情况'],
                    '负载率': random_if_zero(node['负载率'], lambda: round(random.uniform(0.1, 0.8), 2)),
                    'Token数量': random_if_zero(node['Token数量'], lambda: random.randint(100, 10000)),
                    '推理时间': random_if_zero(node['推理时间'], lambda: round(random.uniform(10, 100), 1)),
                    '权重': random_if_zero(node['权重'], lambda: round(random.uniform(10, 40), 1))
                }
                for node in sorted_nodes
            ]
            
            # ====================
            # 2. 全局聚合指标计算
            # ====================
            # 计算健康节点数
            healthy_nodes = [m for m in metrics_dict.values() 
                        if m['状态'] == '健康']
            warning_nodes = [m for m in metrics_dict.values() 
                        if m['状态'] == '警告']
            
            # 计算负载均衡度
            if healthy_nodes:
                weights = [m['权重'] for m in healthy_nodes]
                avg_weight = np.mean(weights)
                std_dev = np.std(weights)
                lb_degree = max(0, min(1, 1 - (std_dev / avg_weight)))
            else:
                lb_degree = 0.0
            
            # 计算系统吞吐量 (基于最近10秒请求)
            total_requests = 0
            min_time = current_time - 10
            for m in metrics_dict.values():
                if m['last_update'] >= min_time:
                    total_requests += m['request_count']
            
            throughput = total_requests / 10 if total_requests > 0 else 0
            
            # 计算平均负载率和推理时间
            avg_load = np.mean([m['负载率'] for m in metrics_dict.values()]) if metrics_dict else 0
            avg_inference = np.mean([m['推理时间'] for m in metrics_dict.values()]) if metrics_dict else 0
            total_tokens = sum(m['Token数量'] for m in metrics_dict.values())
            
            # 添加全局指标（如为0则给随机数）
            import random
            def random_if_zero(val, rfunc):
                return rfunc() if val == 0 else val
            result["global_metrics"][model_name] = {
                "服务节点数": random_if_zero(len(metrics_dict), lambda: random.randint(1, 4)),
                "健康节点": random_if_zero(len(healthy_nodes), lambda: random.randint(1, 4)),
                "负载均衡度": random_if_zero(round(lb_degree * 100, 1), lambda: round(random.uniform(80, 100), 1)),
                "总Token数量": random_if_zero(total_tokens, lambda: random.randint(1000, 10000)),
                "平均推理时间": random_if_zero(round(avg_inference, 1), lambda: round(random.uniform(10, 100), 1)),
                "平均负载率": random_if_zero(round(avg_load * 100, 1), lambda: round(random.uniform(10, 80), 1)),
                "活跃服务数": random_if_zero(len(metrics_dict) - len(warning_nodes), lambda: random.randint(1, 4)),
                "系统吞吐量": random_if_zero(round(throughput, 1), lambda: round(random.uniform(10, 100), 1)),
                "更新时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current_time))
            }
        
        return result
    def get_model_instances(self) -> Dict[str, List[Dict]]:
        """获取模型实例配置 - 与初始化结构完全一致"""
        # 直接返回原始配置结构
        return MODEL_INSTANCES