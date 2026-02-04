from fastapi import APIRouter, HTTPException, Request,Response
from fastapi.responses import StreamingResponse

import httpx
import time
from typing import Dict, Any
from urllib.parse import urlparse
import logging
import requests
import re
import math 

import json
from app.core.model_load_balencer import ModelLoadBalancer
from app.core.config import settings

# 全局大模型负载均衡器实例
model_lb = ModelLoadBalancer()

logging.basicConfig(level=logging.INFO)

# 设置特定日志记录器级别
logger = logging.getLogger("api_gateway")
logger.setLevel(logging.DEBUG)

# 路由定义
router = APIRouter()


@router.get("/modelbase/newapi/data")
async def fetch_new_api_data(request: Request):
    """从 new-api 获取监控数据并透传返回"""
    if not settings.NEW_API_BASE_URL:
        raise HTTPException(status_code=500, detail="NEW_API_BASE_URL 未配置")

    base_url = settings.NEW_API_BASE_URL.rstrip("/")
    target_url = f"{base_url}/api/data/"
    params = dict(request.query_params)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(target_url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=exc.response.status_code,
                detail=f"new-api 响应错误: {exc.response.text}",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"无法连接 new-api: {str(exc)}",
            ) from exc

# 监控端点定义
@router.get("/modelbase/metrics/global")
async def get_model_global_metrics():
    """获取大模型全局监控数据"""
    return model_lb.get_global_metrics()

@router.get("/modelbase/metrics/nodes")
async def get_model_node_metrics():
    """获取大模型节点监控数据（匹配监控图）"""
    return model_lb.get_service_metrics()

@router.get("/modelbase/metrics")
async def get_model_node_metrics():
    """获取大模型节点监控数据（匹配监控图）"""
    return model_lb.get_combined_metrics()


# 大模型请求路由
@router.api_route("/modelbase/{model_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def model_request(
    model_name: str,  # 新增模型名称参数
    path: str, 
    request: Request
):
    """大模型服务路由（自动负载均衡）"""
    logger = logging.getLogger(__name__)
    
    # 获取选择的节点
    try:
        node_id,instance_id = await model_lb.get_next_instance(model_name)  # 传入模型名称
    except Exception as e:
        logger.error(f"获取节点失败: {str(e)}")
        raise HTTPException(status_code=503, detail=f"服务不可用: {str(e)}")
    
    # 构建目标URL
    parsed = urlparse(instance_id)
    if not parsed.scheme:
        base_url = f"http://{instance_id}"
    else:
        base_url = instance_id.rstrip('/')
    
    target_url = f"{base_url}/{path}"
    logger.info(f"目标URL: {target_url}")
    
    # 准备请求
    params = dict(request.query_params)
    headers = dict(request.headers)
    headers.pop("content-length", None) 
    headers.pop("host", None)
    body = await request.body()
    
    start_time = time.time()
    status_code = 200
    try:
        # 检查是否为流式请求
        is_streaming = False
        try:
            request_data = await request.json()
            is_streaming = request_data.get("stream", False)
        except Exception as e:
            logger.warning(f"解析请求体失败: {str(e)}")
        
        if is_streaming:
            async with httpx.AsyncClient() as client:
                try:
                    method = request.method.lower()
                    response = requests.request(
                        method=method,
                        url=target_url,
                        headers=headers,
                        json=request_data,
                        stream=True
                    )
                    
                    # 检查响应状态码
                    if response.status_code != 200:
                        status_code = response.status_code
                        logger.error(f"流式请求错误: {response.status_code} - {response.text}")
                        raise HTTPException(status_code=response.status_code, detail=f"Error: {response.status_code} - {response.text}")
                    
                    # 定义一个生成器函数来处理流式响应
                    async def generate():
                        token_count = 0
                        error_occurred = False
                        decoded_buffer = ""
                        collected_content = []
                        final_token_count = None
                        start_time = time.time()  # 记录请求开始时间
                        
                        try:
                            for chunk in response.iter_content():
                                if chunk:
                                    try:
                                        # 收集原始内容
                                        collected_content.append(chunk)
                                        
                                        # 解码当前chunk
                                        chunk_str = chunk.decode('utf-8')
                                        
                                        # 添加到缓冲区
                                        decoded_buffer += chunk_str
                                        
                                        # 检查是否有完整消息
                                        while '\n' in decoded_buffer:
                                            # 提取一行完整消息
                                            line, _, decoded_buffer = decoded_buffer.partition('\n')
                                            
                                            # 只处理以"data: "开头的行
                                            if line.startswith('data: '):
                                                json_str = line[6:].strip()
                                                
                                                # 尝试解析JSON
                                                try:
                                                    data = json.loads(json_str)
                                                    logger.info(f"接收到数据: {json_str}")
                                                    # 检查是否是最后一行数据（包含metadata.usage）
                                                    if "metadata" in data and "usage" in data["metadata"]:
                                                        # 提取总token数
                                                        usage = data["metadata"]["usage"]
                                                        final_token_count = usage.get("total_tokens", 0)
                                                    # 关键修改：提取usage字段
                                                    if "usage" in data:
                                                        usage = data["usage"]
                                                        final_token_count = usage.get("total_tokens", 0)
                                                        logger.info(f"获取到总Token数: {final_token_count}")
                                                                        
                                                                        
                                                    # 提取Token数量（如果不是最后一行）
                                                    elif "prompt_eval_count" in data and "eval_count" in data:
                                                        token_count += data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
                                                
                                                except json.JSONDecodeError:
                                                    # 如果不是JSON，尝试正则提取
                                                    try:
                                                        prompt_match = re.search(r'"prompt_eval_count":\s*(\d+)', json_str)
                                                        eval_match = re.search(r'"eval_count":\s*(\d+)', json_str)
                                                        
                                                        if prompt_match:
                                                            token_count += int(prompt_match.group(1))
                                                        if eval_match:
                                                            token_count += int(eval_match.group(1))
                                                    except Exception:
                                                        pass
                                    
                                    except UnicodeDecodeError:
                                        # 如果是二进制数据，直接收集
                                        collected_content.append(chunk)
                                    
                                    # 返回原始chunk
                                    yield chunk
                        
                        except Exception as e:
                            error_occurred = True
                            logger.error(f"流错误: {str(e)}")
                            raise
                        finally:
                            # 计算请求耗时（毫秒）
                            resp_time = (time.time() - start_time) * 1000
                            # 向上取整保留两位小数
                            resp_time = math.ceil(resp_time * 100) / 100
                           
                            
                            # 优先使用最后一行数据中的total_tokens
                            if final_token_count is not None:
                                token_count = final_token_count
                            
                            # 如果token_count为0，尝试从完整内容中提取
                            if token_count == 0:
                                try:
                                    # 合并收集的内容
                                    full_content = b"".join(collected_content)
                                    
                                    # 尝试解析完整内容
                                    if b'"prompt_eval_count":' in full_content:
                                        start_idx = full_content.find(b'"prompt_eval_count":') + len(b'"prompt_eval_count":')
                                        end_idx = full_content.find(b',', start_idx)
                                        if end_idx == -1:
                                            end_idx = full_content.find(b'}', start_idx)
                                        prompt_tokens = int(full_content[start_idx:end_idx].strip())
                                        token_count += prompt_tokens
                                  
                                    if b'"eval_count":' in full_content:
                                        start_idx = full_content.find(b'"eval_count":') + len(b'"eval_count":')
                                        end_idx = full_content.find(b',', start_idx)
                                        if end_idx == -1:
                                            end_idx = full_content.find(b'}', start_idx)
                                        completion_tokens = int(full_content[start_idx:end_idx].strip())
                                        token_count += completion_tokens
                                except Exception as e:
                                    logger.warning(f"从完整内容提取token失败: {str(e)}")
                            
                            # 流结束后更新指标
                            logger.info(f"流式请求结束 - Token数量: {token_count}, 耗时: {resp_time:.2f}ms")
                            
                            # 更新负载均衡指标
                            await model_lb.update_instance_metrics(
                                model_name,
                                node_id,
                                resp_time,
                                is_error=error_occurred or response.status_code >= 400,
                                token_count=token_count
                            )
                                
                    # 返回流式响应给前端
                    return StreamingResponse(
                        generate(),
                        media_type=response.headers.get('Content-Type', 'text/event-stream'),
                        status_code=response.status_code
                    )
                except httpx.RequestError as e:
                    logger.error(f"流式请求HTTPX错误: {str(e)}")
                    raise HTTPException(status_code=502, detail=f"模型服务错误: {str(e)}")
                except Exception as e:
                    logger.error(f"流式请求其他错误: {str(e)}")
                    raise HTTPException(status_code=500, detail=f"内部服务器错误: {str(e)}")
        else:
            logger.info("处理非流式请求")
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.request(
                        method=request.method,
                        url=target_url,
                        params=params,
                        headers=headers,
                        content=body,
                        timeout=300
                    )
                    
                    logger.info(f"非流式请求状态码: {response.status_code}")
                    
                    # 获取响应内容
                    response_content = response.content
                    
                    # 简单提取 token 值
                    token_count = 0
                    
                    # 方法1：查找特定字段
                    if b'"prompt_eval_count":' in response_content:
                        # 提取 prompt_eval_count 值
                        start_idx = response_content.find(b'"prompt_eval_count":') + len(b'"prompt_eval_count":')
                        end_idx = response_content.find(b',', start_idx)
                        if end_idx == -1:
                            end_idx = response_content.find(b'}', start_idx)
                        prompt_tokens = int(response_content[start_idx:end_idx].strip())
                        token_count += prompt_tokens
                    
                     # 关键修改：提取usage字段
                    if "usage" in data:
                        usage = data["usage"]
                        token_count = usage.get("total_tokens", 0)
                        logger.info(f"获取到总Token数: {final_token_count}")
                    
                    if b'"eval_count":' in response_content:
                        # 提取 eval_count 值
                        start_idx = response_content.find(b'"eval_count":') + len(b'"eval_count":')
                        end_idx = response_content.find(b',', start_idx)
                        if end_idx == -1:
                            end_idx = response_content.find(b'}', start_idx)
                        completion_tokens = int(response_content[start_idx:end_idx].strip())
                        token_count += completion_tokens
                        
                    logger.info(f"提取的 token 数量: {token_count}")
                    
                    # 更新指标
                    resp_time = (time.time() - start_time) * 1000  # 毫秒
                    await model_lb.update_instance_metrics(
                        model_name,  # 传入模型名称
                        instance_id,
                        resp_time,
                        is_error=(response.status_code >= 400),
                        token_count=token_count
                    )
                    
                    return Response(
                        content=response.content,
                        status_code=response.status_code,
                        headers=dict(response.headers)
                    )
                except httpx.RequestError as e:
                    logger.error(f"非流式请求HTTPX错误: {str(e)}")
                    raise HTTPException(status_code=502, detail=f"模型服务错误: {str(e)}")
                except Exception as e:
                    logger.error(f"非流式请求其他错误: {str(e)}")
                    raise HTTPException(status_code=500, detail=f"内部服务器错误: {str(e)}")
    
    except Exception as e:
        # 处理请求异常
        resp_time = (time.time() - start_time) * 1000
        await model_lb.update_instance_metrics(
            model_name,  # 传入模型名称
            instance_id,
            resp_time,
            is_error=True
        )
        
        if isinstance(e, httpx.TimeoutException):
            logger.error(f"请求超时: {str(e)}")
            raise HTTPException(status_code=504, detail="模型请求超时")
        elif isinstance(e, HTTPException):
            logger.error(f"HTTP异常: {str(e)}")
            raise
        else:
            logger.error(f"未知错误: {str(e)}")
            raise HTTPException(status_code=500, detail=f"内部服务器错误: {str(e)}")
