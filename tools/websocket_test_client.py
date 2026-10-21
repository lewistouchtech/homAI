"""
WebSocket测试客户端 - 用于验证实时通信功能
"""

import asyncio
import websockets
import json
import logging
from datetime import datetime

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WebSocketTestClient:
    def __init__(self, uri):
        self.uri = uri
        self.websocket = None
        self.connected = False
    
    async def connect(self):
        """连接到WebSocket服务器"""
        try:
            self.websocket = await websockets.connect(self.uri)
            self.connected = True
            logger.info(f"成功连接到WebSocket服务器: {self.uri}")
            
            # 启动消息监听器
            asyncio.create_task(self.listen_for_messages())
            
            return True
        except Exception as e:
            logger.error(f"连接WebSocket服务器失败: {e}")
            return False
    
    async def disconnect(self):
        """断开连接"""
        if self.websocket:
            await self.websocket.close()
            self.connected = False
            logger.info("已断开WebSocket连接")
    
    async def listen_for_messages(self):
        """监听来自服务器的消息"""
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    logger.info(f"收到服务器消息: {data}")
                    
                    # 根据消息类型进行处理
                    if data.get('type') == 'ping':
                        # 回复pong
                        await self.send_message({
                            'type': 'pong',
                            'timestamp': datetime.now().isoformat()
                        })
                    elif data.get('type') == 'sync_request':
                        # 处理同步请求
                        await self.handle_sync_request(data)
                        
                except json.JSONDecodeError:
                    logger.info(f"收到文本消息: {message}")
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket连接已关闭")
            self.connected = False
        except Exception as e:
            logger.error(f"监听消息时出错: {e}")
            self.connected = False
    
    async def send_message(self, message):
        """发送消息到服务器"""
        if self.websocket and self.connected:
            try:
                await self.websocket.send(json.dumps(message, ensure_ascii=False))
                logger.info(f"发送消息: {message}")
                return True
            except Exception as e:
                logger.error(f"发送消息失败: {e}")
                self.connected = False
                return False
        else:
            logger.error("WebSocket未连接")
            return False
    
    async def handle_sync_request(self, data):
        """处理同步请求"""
        logger.info(f"处理同步请求: {data}")
        # 这里可以实现具体的同步逻辑
        response = {
            'type': 'sync_response',
            'request_id': data.get('request_id'),
            'status': 'processed',
            'timestamp': datetime.now().isoformat()
        }
        await self.send_message(response)
    
    async def test_real_time_communication(self):
        """测试实时通信功能"""
        if not self.connected:
            logger.error("WebSocket未连接，无法测试通信")
            return False
        
        # 发送测试消息
        test_message = {
            'type': 'test_message',
            'content': 'Hello from test client!',
            'timestamp': datetime.now().isoformat(),
            'test_id': 'real_time_comm_test_001'
        }
        
        await self.send_message(test_message)
        
        # 等待响应
        await asyncio.sleep(2)
        
        # 发送同步测试消息
        sync_message = {
            'type': 'sync_test',
            'data': {
                'source': 'client',
                'destination': 'server',
                'operation': 'data_sync',
                'payload': {
                    'timestamp': datetime.now().isoformat(),
                    'data': 'test_sync_data'
                }
            },
            'timestamp': datetime.now().isoformat()
        }
        
        await self.send_message(sync_message)
        
        return True

async def main():
    # WebSocket服务器地址 - 使用服务器启动时显示的地址
    # 从之前的日志可以看出，WebSocket地址是 ws://YOUR_DEVICE_HOST:8000/homebrain/v1/
    # 但我们需要使用本地地址进行测试
    websocket_uri = "ws://localhost:8000/homebrain/v1/"  # 默认端口8000
    
    # 尝试连接到服务器显示的WebSocket地址
    try:
        # 从日志中获取实际的WebSocket地址
        import subprocess
        result = subprocess.run(['grep', 'Websocket地址', '/tmp/homebrain_server.log'], 
                              capture_output=True, text=True)
        if result.returncode == 0 and 'ws://' in result.stdout:
            # 提取WebSocket地址
            import re
            ws_match = re.search(r'ws://[^/]+/homebrain/v1/', result.stdout)
            if ws_match:
                websocket_uri = ws_match.group(0)
                logger.info(f"使用从服务器日志获取的WebSocket地址: {websocket_uri}")
    except:
        pass
    
    logger.info(f"尝试连接到WebSocket服务器: {websocket_uri}")
    
    client = WebSocketTestClient(websocket_uri)
    
    # 连接到服务器
    if await client.connect():
        logger.info("WebSocket连接成功，开始测试实时通信...")
        
        # 测试实时通信
        await client.test_real_time_communication()
        
        # 保持连接一段时间以观察响应
        await asyncio.sleep(10)
        
        # 断开连接
        await client.disconnect()
    else:
        logger.error("无法连接到WebSocket服务器")
        return False
    
    return True

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n测试已中断")
    except Exception as e:
        print(f"测试出错: {e}")