# 小智服务器风格的 reload 方法实现
# 统一使用 update_config() 处理配置更新

    async def reload_asr_module(self):
        """重新加载ASR模块（小智服务器风格：调用统一的update_config）"""
        self.logger.bind(tag=TAG).info("开始重新加载ASR模块...")
        try:
            # 使用统一的update_config方法，它会自动检测ASR类型变化
            result = await self.update_config()
            if result:
                self.logger.bind(tag=TAG).info("ASR模块重新加载成功")
            else:
                self.logger.bind(tag=TAG).error("ASR模块重新加载失败")
            return result
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"重新加载ASR模块失败: {str(e)}")
            import traceback
            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            return False

    async def reload_llm_module(self):
        """重新加载LLM模块（小智服务器风格：调用统一的update_config）"""
        self.logger.bind(tag=TAG).info("开始重新加载LLM模块...")
        try:
            # 使用统一的update_config方法，它会自动检测LLM类型变化
            result = await self.update_config()
            if result:
                self.logger.bind(tag=TAG).info("LLM模块重新加载成功")
            else:
                self.logger.bind(tag=TAG).error("LLM模块重新加载失败")
            return result
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"重新加载LLM模块失败: {str(e)}")
            import traceback
            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            return False

    async def reload_vad_module(self):
        """重新加载VAD模块（小智服务器风格：调用统一的update_config）"""
        self.logger.bind(tag=TAG).info("开始重新加载VAD模块...")
        try:
            # 使用统一的update_config方法，它会自动检测VAD类型变化
            result = await self.update_config()
            if result:
                self.logger.bind(tag=TAG).info("VAD模块重新加载成功")
            else:
                self.logger.bind(tag=TAG).error("VAD模块重新加载失败")
            return result
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"重新加载VAD模块失败: {str(e)}")
            import traceback
            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            return False
