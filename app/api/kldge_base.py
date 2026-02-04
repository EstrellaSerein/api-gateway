import requests
import json
import math
import os
import time
import subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# Prometheus 配置
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

# ==========================================
# 1. 定义数据模型
# ==========================================

class NodeOverview(BaseModel):
    physical_nodes: int | str
    virtual_nodes: int | str
    total_qps: float | str

class PerformanceMetrics(BaseModel):
    avg_response_time: float | str
    error_rate: float | str
    active_connections: int | str
    system_load: float | str

class NodeMetrics(BaseModel):
    instance: str
    weight: float
    target_weight: float
    connections: int | str
    qps: float | str
    response_time: float | str

class MetricsResponse(BaseModel):
    node_overview: NodeOverview
    performance_metrics: PerformanceMetrics
    realtime_monitoring: list[NodeMetrics]
    timestamp: str
    query_time_ms: int

# ==========================================
# 2. 辅助工具函数
# ==========================================

def query_prometheus(query):
    """执行 Prometheus 查询并返回结果"""
    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={'query': query, 'time': time.time()},
            timeout=2
        )
        response.raise_for_status()
        data = response.json()
        if data['status'] == 'success':
            return data['data']['result']
    except Exception as e:
        print(f"--- [DEBUG] Prometheus 查询失败: {query}, 错误: {e} ---", flush=True)
    return []

def format_value(value):
    """格式化数值，保留2位小数"""
    try:
        val = float(value)
        if math.isnan(val) or math.isinf(val):
            return 0
        if val.is_integer():
            return int(val)
        return round(val, 2)
    except (ValueError, TypeError):
        return 0

def validate_and_clean_data(data):
    """递归清理数据中的 NaN 和 Infinity"""
    if isinstance(data, dict):
        return {k: validate_and_clean_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [validate_and_clean_data(i) for i in data]
    elif isinstance(data, float):
        if math.isnan(data) or math.isinf(data):
            return 0
        return data
    return data

# ==========================================
# 3. 核心业务逻辑
# ==========================================

def get_docker_stats():
    """
    使用系统命令 'docker ps' 获取容器列表
    返回: (pg_count, weaviate_count, first_pg_name)
    """
    try:
        cmd = ["docker", "ps", "--format", "{{.Image}}@{{.Names}}"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            return 0, 0, "Unknown-Node"

        output = result.stdout.strip()
        if not output:
            return 0, 0, "Unknown-Node"
            
        lines = output.split('\n')
        
        pg_count = 0
        weaviate_count = 0
        first_pg_name = "postgres-node-01" # 默认值

        for line in lines:
            line = line.strip()
            if not line or '@' not in line: 
                continue
                
            parts = line.split('@')
            image_name = parts[0].lower()
            container_name = parts[1]
            
            if 'postgres' in image_name:
                pg_count += 1
                if pg_count == 1:
                    first_pg_name = container_name
            elif 'weaviate' in image_name:
                weaviate_count += 1
            
        return pg_count, weaviate_count, first_pg_name

    except Exception as e:
        print(f"--- [DEBUG] Docker 扫描出错: {e} ---", flush=True)
        return 0, 0, "Unknown-Node"

def get_node_overview():
    """获取节点概览"""
    pg_count, weaviate_count, _ = get_docker_stats()
    
    total_qps = 0
    qps_data = query_prometheus('sum(rate(nginx_http_response_count_total[5m]))')
    if qps_data and len(qps_data) > 0:
        total_qps = format_value(qps_data[0]['value'][1])

    return {
        "physical_nodes": pg_count,
        "virtual_nodes": weaviate_count,
        "total_qps": total_qps
    }

def get_performance_metrics():
    """
    获取性能指标
    """
    metrics = {
        "avg_response_time": 0,
        "error_rate": 0,
        "active_connections": 0,
        "system_load": 0
    }
    
    # 1. 平均响应时间
    resp_time = query_prometheus('sum(rate(nginx_http_upstream_time_seconds_sum{status="200"}[15m]))/sum(rate(nginx_http_upstream_time_seconds_count{status="200"}[15m]))')
    if resp_time: metrics["avg_response_time"] = format_value(resp_time[0]['value'][1])

    # 2. 错误率
    err_rate = query_prometheus('sum(rate(nginx_http_response_count_total{status=~"5.."}[15m])) / sum(rate(nginx_http_response_count_total[15m])) * 100')
    if err_rate: metrics["error_rate"] = format_value(err_rate[0]['value'][1])

    # 3. 活跃连接数 (使用日志估算并发数)
    # 【第一顺位】直接查询 Prometheus 里的真实指标 (也就是你截图里查到的那个)
    real_active = query_prometheus('sum(nginx_connections_active)')
    
    if real_active:
        # 如果查到了，直接用！(截图里是 3，这里就会拿到 3)
        metrics["active_connections"] = int(float(real_active[0]['value'][1]))
    else:
        # 【第二顺位】如果没查到，再进行估算 (Little's Law 保底)
        concurrency_data = query_prometheus('sum(rate(nginx_http_request_duration_seconds_sum[15m]))')
        calculated_conns = 0.0
        if concurrency_data:
            calculated_conns = float(concurrency_data[0]['value'][1])
        
        # 简单的保底逻辑
        if calculated_conns >= 1:
            metrics["active_connections"] = int(calculated_conns)
        elif calculated_conns > 0.001:
            metrics["active_connections"] = 1
        else:
            # 再看看有没有 QPS，有QPS至少给个1
            qps_check = query_prometheus('sum(rate(nginx_http_response_count_total[15m]))')
            if qps_check and float(qps_check[0]['value'][1]) > 0:
                 metrics["active_connections"] = 1
            else:
                 metrics["active_connections"] = 0
    
    # 4. 系统负载 (CPU使用率)
    cpu_usage = query_prometheus('100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[15m])) * 100)')
    if cpu_usage:
        metrics["system_load"] = format_value(cpu_usage[0]['value'][1])
    else:
        load_avg = query_prometheus('avg(node_load1)')
        if load_avg: metrics["system_load"] = format_value(load_avg[0]['value'][1])

    return metrics

def assemble_realtime_metrics(node_overview, performance_metrics):
    """
    【核心修复】
    不再独立查询，而是直接【复用】上面的总数据。
    这样保证：Top(1) == Bottom(1)，绝不出现上面是1下面是0的情况。
    """
    # 再次获取 Docker 名字，让列表显示真实的容器名 (如 docker_fengfz-db-1)
    pg_count, _, first_pg_name = get_docker_stats()
    
    instance_name = first_pg_name if pg_count > 0 else "No-Active-Node"
    
    # 构造单个节点，直接引用 performance_metrics 的值
    single_node = {
        "instance": instance_name,
        "weight": 100, 
        "target_weight": 100,
        # 👇👇👇 关键点：直接赋值 👇👇👇
        "connections": performance_metrics.get("active_connections", 0),
        "qps": node_overview.get("total_qps", 0),
        "response_time": performance_metrics.get("avg_response_time", 0)
    }
    
    return [single_node]

# ==========================================
# 4. API 路由入口
# ==========================================

@router.get("/kldgebase/metrics", response_model=MetricsResponse)
def get_metrics():
    """获取所有指标数据"""
    try:
        start_time = time.time()
        
        # 1. 先计算总指标
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_overview = executor.submit(get_node_overview)
            future_performance = executor.submit(get_performance_metrics)
            
            node_overview = future_overview.result()
            performance_metrics = future_performance.result()

        # 2. 【关键】用总指标组装列表，确保数据一致性
        realtime_monitoring = assemble_realtime_metrics(node_overview, performance_metrics)

        query_time_ms = int((time.time() - start_time) * 1000)
        timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

        response_data = {
            "node_overview": node_overview,
            "performance_metrics": performance_metrics,
            "realtime_monitoring": realtime_monitoring,
            "timestamp": timestamp,
            "query_time_ms": query_time_ms
        }

        return validate_and_clean_data(response_data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))