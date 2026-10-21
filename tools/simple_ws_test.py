#!/usr/bin/env python3
"""
简单WebSocket测试 - 验证WebSocket服务器是否正常工作
"""

import asyncio
import websockets
import json
import sys

async def test_websocket_connection():
    """测试WebSocket连接"""
    uri = "ws://localhost:8000/homebrain/v1/"
    
    print(f"尝试连接到WebSocket服务器: {uri}")
    
    try:
        # 尝试连接
        websocket = await websockets.connect(uri, timeout=10)
        print("✅ WebSocket连接成功!")
        
        # 发送一个简单的测试消息
        test_message = {
            "type": "test",
            "content": "WebSocket connection test",
            "timestamp": "2026-04-12T01:16:00"
        }
        
        await websocket.send(json.dumps(test_message))
        print(f"📤 发送测试消息: {test_message}")
        
        # 尝试接收响应（设置超时）
        try:
            response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            print(f"📥 收到响应: {response}")
        except asyncio.TimeoutError:
            print("⏰ 未收到即时响应（这可能是正常的）")
        
        # 关闭连接
        await websocket.close()
        print("✅ 连接已关闭")
        
        return True
        
    except websockets.exceptions.InvalidStatusCode as e:
        print(f"❌ 连接失败 - 无效状态码: {e}")
        # 这可能是因为需要特定的headers或认证
        return False
    except OSError as e:
        print(f"❌ 连接失败 - 网络错误: {e}")
        return False
    except Exception as e:
        print(f"❌ 连接失败 - 未知错误: {e}")
        return False

async def main():
    print("🔍 WebSocket服务器连接测试")
    print("="*50)
    
    success = await test_websocket_connection()
    
    print("="*50)
    if success:
        print("🎉 WebSocket通信验证: ✅ 通过")
        return True
    else:
        print("⚠️ WebSocket通信验证: ❌ 需要额外配置")
        return False

if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result else 1)