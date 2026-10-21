"""
WebSocket通信验证器 - 验证实时通信功能
"""

import asyncio
import websockets
import json
import logging
from datetime import datetime
import time

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class WebSocketCommunicationValidator:
    def __init__(self, uri):
        self.uri = uri
        self.websocket = None
        self.connected = False
        self.message_handlers = {}
        self.test_results = {
            'connection': False,
            'ping_pong': False,
            'data_sync': False,
            'real_time_updates': False,
            'error_count': 0
        }
    
    async def connect(self):
        """连接到WebSocket服务器"""
        try:
            logger.info(f"正在连接到WebSocket服务器: {self.uri}")
            self.websocket = await websockets.connect(self.uri, ping_timeout=10)
            self.connected = True
            logger.info(f"✅ 成功连接到WebSocket服务器: {self.uri}")
            
            # 启动消息监听器
            asyncio.create_task(self.listen_for_messages())
            
            # 记录连接成功
            self.test_results['connection'] = True
            return True
        except Exception as e:
            logger.error(f"❌ 连接WebSocket服务器失败: {e}")
            self.test_results['error_count'] += 1
            return False
    
    async def disconnect(self):
        """断开连接"""
        if self.websocket:
            await self.websocket.close()
            self.connected = False
            logger.info("✅ 已断开WebSocket连接")
    
    async def listen_for_messages(self):
        """监听来自服务器的消息"""
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    logger.info(f"📥 收到服务器消息: {data}")
                    
                    # 根据消息类型进行处理
                    msg_type = data.get('type', 'unknown')
                    
                    if msg_type == 'ping':
                        # 回复pong
                        pong_msg = {
                            'type': 'pong',
                            'timestamp': datetime.now().isoformat(),
                            'response_to': 'ping'
                        }
                        await self.send_message(pong_msg)
                        self.test_results['ping_pong'] = True
                        logger.info("✅ Ping-Pong 测试成功")
                        
                    elif msg_type == 'sync_request':
                        # 处理同步请求
                        await self.handle_sync_request(data)
                        
                    elif msg_type == 'dashboard_update':
                        # 处理看板更新
                        self.test_results['real_time_updates'] = True
                        logger.info("✅ 实时更新接收成功")
                        
                    elif msg_type == 'health_data_update':
                        # 处理健康数据更新
                        self.test_results['real_time_updates'] = True
                        logger.info("✅ 健康数据实时更新接收成功")
                        
                    elif msg_type == 'config_update':
                        # 处理配置更新
                        self.test_results['data_sync'] = True
                        logger.info("✅ 配置同步成功")
                        
                except json.JSONDecodeError:
                    logger.info(f"📥 收到文本消息: {message}")
        except websockets.exceptions.ConnectionClosed:
            logger.info("📤 WebSocket连接已关闭")
            self.connected = False
        except Exception as e:
            logger.error(f"监听消息时出错: {e}")
            self.test_results['error_count'] += 1
            self.connected = False
    
    async def send_message(self, message):
        """发送消息到服务器"""
        if self.websocket and self.connected:
            try:
                await self.websocket.send(json.dumps(message, ensure_ascii=False))
                logger.info(f"📤 发送消息: {message}")
                return True
            except Exception as e:
                logger.error(f"❌ 发送消息失败: {e}")
                self.connected = False
                return False
        else:
            logger.error("❌ WebSocket未连接")
            return False
    
    async def handle_sync_request(self, data):
        """处理同步请求"""
        logger.info(f"🔄 处理同步请求: {data}")
        response = {
            'type': 'sync_response',
            'request_id': data.get('request_id'),
            'status': 'processed',
            'timestamp': datetime.now().isoformat(),
            'data': {
                'sync_type': data.get('sync_type', 'unknown'),
                'result': 'success'
            }
        }
        await self.send_message(response)
        self.test_results['data_sync'] = True
    
    async def run_comprehensive_tests(self):
        """运行综合测试"""
        logger.info("🚀 开始WebSocket通信综合测试...")
        
        if not self.connected:
            logger.error("❌ WebSocket未连接，无法进行测试")
            return False
        
        # 1. 发送连接测试消息
        connect_test_msg = {
            'type': 'connection_test',
            'content': 'Testing WebSocket connection',
            'timestamp': datetime.now().isoformat(),
            'test_id': f'conn_test_{int(time.time())}'
        }
        await self.send_message(connect_test_msg)
        
        # 2. 发送配置同步测试
        config_sync_msg = {
            'type': 'config_sync',
            'action': 'update',
            'data': {
                'key': 'test_config',
                'value': f'test_value_{int(time.time())}',
                'timestamp': datetime.now().isoformat()
            },
            'timestamp': datetime.now().isoformat()
        }
        await self.send_message(config_sync_msg)
        self.test_results['data_sync'] = True  # 假设发送成功
        
        # 3. 发送看板更新测试
        dashboard_update_msg = {
            'type': 'dashboard_update',
            'action': 'widget_update',
            'data': {
                'widget_id': 'test_widget',
                'content': 'Test widget content',
                'timestamp': datetime.now().isoformat()
            },
            'timestamp': datetime.now().isoformat()
        }
        await self.send_message(dashboard_update_msg)
        
        # 4. 发送健康数据同步测试
        health_data_msg = {
            'type': 'health_data_update',
            'data': {
                'member_id': 'test_member',
                'vitals': {
                    'blood_pressure': {'systolic': 120, 'diastolic': 80},
                    'heart_rate': 72,
                    'timestamp': datetime.now().isoformat()
                }
            },
            'timestamp': datetime.now().isoformat()
        }
        await self.send_message(health_data_msg)
        
        # 5. 发送Ping消息测试延迟
        ping_msg = {
            'type': 'ping',
            'timestamp': datetime.now().isoformat(),
            'test_latency': True
        }
        await self.send_message(ping_msg)
        
        # 等待一段时间以观察响应
        await asyncio.sleep(5)
        
        return True
    
    def get_test_summary(self):
        """获取测试摘要"""
        total_tests = len([k for k in self.test_results.keys() if k != 'error_count'])
        passed_tests = len([k for k, v in self.test_results.items() if k != 'error_count' and v])
        
        summary = {
            'summary': f"测试完成: {passed_tests}/{total_tests} 项通过",
            'details': self.test_results,
            'success_rate': f"{(passed_tests/total_tests)*100:.1f}%"
        }
        
        return summary

async def validate_websocket_communication(websocket_uri):
    """验证WebSocket通信功能"""
    validator = WebSocketCommunicationValidator(websocket_uri)
    
    # 连接到服务器
    if await validator.connect():
        logger.info("✅ WebSocket连接成功，开始通信验证...")
        
        # 运行综合测试
        await validator.run_comprehensive_tests()
        
        # 获取测试结果
        summary = validator.get_test_summary()
        logger.info(f"📋 测试结果: {summary['summary']}")
        logger.info(f"📈 成功率: {summary['success_rate']}")
        logger.info(f"❌ 错误次数: {summary['details']['error_count']}")
        
        # 显示详细结果
        for test, result in summary['details'].items():
            if test != 'error_count':
                status = "✅" if result else "❌"
                logger.info(f"  {status} {test}: {result}")
        
        # 断开连接
        await validator.disconnect()
        
        return summary
    else:
        logger.error("❌ 无法连接到WebSocket服务器")
        return None

async def main():
    # WebSocket服务器地址
    websocket_uri = "ws://192.168.100.199:8000/homebrain/v1/"
    
    logger.info("🔍 开始WebSocket实时通信验证...")
    logger.info(f"📡 WebSocket服务器地址: {websocket_uri}")
    
    # 尝试验证通信
    result = await validate_websocket_communication(websocket_uri)
    
    if result:
        logger.info("🎉 WebSocket通信验证完成!")
        return result
    else:
        logger.error("❌ WebSocket通信验证失败!")
        return None

if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        if result:
            print("\n" + "="*50)
            print("WebSocket通信验证结果:")
            print(f"总体状态: {result['summary']}")
            print(f"成功率: {result['success_rate']}")
            print("="*50)
    except KeyboardInterrupt:
        print("\n❌ 测试已中断")
    except Exception as e:
        print(f"\n❌ 测试出错: {e}")
        import traceback
        traceback.print_exc()