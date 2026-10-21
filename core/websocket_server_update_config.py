    async def update_config(self) -> bool:
        """更新服务器配置并重新初始化组件（从数据库读取配置）

        Returns:
            bool: 更新是否成功
        """
        try:
            async with self.config_lock:
                self.logger.bind(tag=TAG).info("开始更新配置...")
                
                # 从数据库读取配置并合并到当前配置
                # 使用深拷贝，避免修改 new_config 时影响 self.config（否则 check_asr_update 等无法检测变化）
                import copy
                new_config = copy.deepcopy(self.config)
                try:
                    import sqlite3
                    from pathlib import Path
                    db_path = Path(__file__).parent.parent / "data" / "devices.db"
                    if db_path.exists():
                        conn = sqlite3.connect(db_path, timeout=30.0)
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("PRAGMA busy_timeout=30000")
                        cursor = conn.cursor()
                        
                        # 读取 ASR 配置
                        cursor.execute("SELECT config_key, config_value FROM asr_config")
                        asr_rows = cursor.fetchall()
                        if asr_rows:
                            asr_module = new_config.get("selected_module", {}).get("ASR", "FunASR")
                            if "ASR" in new_config and asr_module in new_config["ASR"]:
                                for row in asr_rows:
                                    new_config["ASR"][asr_module][row[0]] = row[1]
                                self.logger.bind(tag=TAG).info(f"合并 ASR 配置: {dict(asr_rows)}")
                        
                        # 读取 LLM 配置
                        cursor.execute("SELECT config_key, config_value FROM llm_config")
                        llm_rows = cursor.fetchall()
                        if llm_rows:
                            llm_module = new_config.get("selected_module", {}).get("LLM", "Doubao")
                            if "LLM" in new_config and llm_module in new_config["LLM"]:
                                for row in llm_rows:
                                    new_config["LLM"][llm_module][row[0]] = row[1]
                                self.logger.bind(tag=TAG).info(f"合并 LLM 配置: {dict(llm_rows)}")
                        
                        # 读取 VAD 配置
                        cursor.execute("SELECT config_key, config_value FROM vad_config")
                        vad_rows = cursor.fetchall()
                        if vad_rows:
                            vad_module = new_config.get("selected_module", {}).get("VAD", "Silero")
                            if "VAD" in new_config and vad_module in new_config["VAD"]:
                                for row in vad_rows:
                                    new_config["VAD"][vad_module][row[0]] = row[1]
                                self.logger.bind(tag=TAG).info(f"合并 VAD 配置: {dict(vad_rows)}")

                        # 读取 TTS 配置
                        cursor.execute("SELECT config_key, config_value FROM tts_config")
                        tts_rows = cursor.fetchall()
                        if tts_rows:
                            tts_module = new_config.get("selected_module", {}).get("TTS", "ChatTTS")
                            if "TTS" in new_config and tts_module in new_config["TTS"]:
                                for row in tts_rows:
                                    new_config["TTS"][tts_module][row[0]] = row[1]
                                self.logger.bind(tag=TAG).info(f"合并 TTS 配置: {dict(tts_rows)}")

                        cursor.close()
                        conn.close()
                except Exception as db_err:
                    self.logger.bind(tag=TAG).warning(f"读取数据库配置失败: {db_err}")
                
                # 检查 VAD、ASR、TTS 类型是否需要更新
                update_vad = check_vad_update(self.config, new_config)
                update_asr = check_asr_update(self.config, new_config)
                update_tts = check_tts_update(self.config, new_config)
                self.logger.bind(tag=TAG).info(
                    f"检查组件类型是否需要更新: VAD={update_vad}, ASR={update_asr}, TTS={update_tts}"
                )

                # 更新配置
                self.config = new_config

                # 关键修复：确保 ASR 配置使用数据库中的最新 model 值
                # 因为 initialize_asr 在 reload 时 skip_db=True，直接使用内存配置
                # 所以必须在这里把数据库的 model 同步到内存配置
                try:
                    import sqlite3
                    from pathlib import Path
                    db_path = Path(__file__).parent.parent / "data" / "devices.db"
                    if db_path.exists():
                        conn = sqlite3.connect(db_path, timeout=30.0)
                        conn.execute("PRAGMA journal_mode=WAL")
                        cursor = conn.cursor()
                        cursor.execute("SELECT config_key, config_value FROM asr_config")
                        rows = cursor.fetchall()
                        db_asr_config = {row[0]: row[1] for row in rows}

                        # 获取当前 ASR 模块
                        asr_module = new_config.get("selected_module", {}).get("ASR", "FunASR")
                        if "ASR" in new_config and asr_module in new_config["ASR"]:
                            # 同步数据库配置到内存配置
                            if "model" in db_asr_config:
                                new_config["ASR"][asr_module]["model_dir"] = db_asr_config["model"]
                                self.logger.bind(tag=TAG).info(f"[update_config] 同步数据库 model 到内存配置: {db_asr_config['model']}")

                        cursor.close()
                        conn.close()
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"同步数据库配置到内存失败: {e}")

                # 重新初始化组件
                modules = initialize_modules(
                    self.logger,
                    new_config,
                    update_vad,
                    update_asr,
                    "LLM" in new_config["selected_module"],
                    update_tts,
                    "Memory" in new_config["selected_module"],
                    "Intent" in new_config["selected_module"],
                )

                # 更新组件实例
                if "vad" in modules:
                    self._vad = modules["vad"]
                    self.logger.bind(tag=TAG).info("VAD模块已更新")
                if "asr" in modules:
                    self._asr = modules["asr"]
                    self.logger.bind(tag=TAG).info("ASR模块已更新")
                if "llm" in modules:
                    self._llm = modules["llm"]
                    self.logger.bind(tag=TAG).info("LLM模块已更新")
                if "tts" in modules:
                    self._tts = modules["tts"]
                    self.logger.bind(tag=TAG).info("TTS模块已更新")
                if "intent" in modules:
                    self._intent = modules["intent"]
                    self.logger.bind(tag=TAG).info("Intent模块已更新")
                if "memory" in modules:
                    self._memory = modules["memory"]
                    self.logger.bind(tag=TAG).info("Memory模块已更新")
                
                self.logger.bind(tag=TAG).info("配置更新任务执行完毕")
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新服务器配置失败: {str(e)}")
            import traceback
            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            return False
