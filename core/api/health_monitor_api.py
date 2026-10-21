"""
健康监测API端点

该模块提供HTTP API端点来处理健康监测数据的接收和查询。
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, Dict, Any
import asyncio

from plugins_func.functions.health_monitor import (
    process_health_device_data,
    get_health_history,
    get_current_vital_signs,
    register_health_device,
    check_health_alerts,
    register_health_alert_callback,
    voice_alert_callback
)

router = APIRouter(prefix="/api/health", tags=["health"])

# 请求模型
class HealthDataRequest(BaseModel):
    device_type: str
    device_id: str
    data: Dict[str, Any]
    timestamp: Optional[str] = None

class HealthHistoryRequest(BaseModel):
    device_type: str
    limit: Optional[int] = 10
    device_id: Optional[str] = None

class DeviceInfo(BaseModel):
    type: str
    name: Optional[str] = None
    description: Optional[str] = None

class RegisterDeviceRequest(BaseModel):
    device_id: str
    device_info: DeviceInfo

# 注册语音提醒回调
register_health_alert_callback(voice_alert_callback)

@router.post("/data")
async def receive_health_data(request: HealthDataRequest):
    """接收健康监测设备数据"""
    try:
        # 构建健康数据对象
        health_data = {
            "device_type": request.device_type,
            "device_id": request.device_id,
            "data": request.data,
            "timestamp": request.timestamp
        }
        
        # 处理健康数据
        result = await process_health_device_data(health_data)
        
        if result["status"] == "success":
            # 检查是否有健康提醒
            alerts = await check_health_alerts(request.device_id)
            if alerts:
                result["alerts"] = alerts
                
            return result
        else:
            raise HTTPException(status_code=400, detail=result["message"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理健康数据时出错: {str(e)}")

@router.get("/history")
async def query_health_history(device_type: str, limit: int = 10, device_id: str = None):
    """查询健康数据历史记录"""
    try:
        result = get_health_history(device_type, limit, device_id)
        
        if result["status"] == "success":
            return result
        else:
            raise HTTPException(status_code=400, detail=result["message"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询健康历史数据时出错: {str(e)}")

@router.get("/current")
async def get_current_health_status(device_id: str = None):
    """获取当前生命体征数据"""
    try:
        result = get_current_vital_signs(device_id)
        
        if result["status"] == "success":
            return result
        else:
            raise HTTPException(status_code=400, detail=result["message"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取当前生命体征数据时出错: {str(e)}")

@router.post("/register_device")
async def register_device(request: RegisterDeviceRequest):
    """注册健康监测设备"""
    try:
        result = register_health_device(request.device_id, request.device_info.dict())
        
        if result["status"] == "success":
            return result
        else:
            raise HTTPException(status_code=400, detail=result["message"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"注册设备时出错: {str(e)}")

# 为兼容现有系统，也提供一个通用的健康数据处理函数
async def handle_health_data_webhook(payload: dict):
    """处理来自Webhook的健康数据"""
    return await process_health_device_data(payload)