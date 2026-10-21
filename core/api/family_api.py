"""
家庭成员管理API（完整版）
与控制台数据格式匹配
"""
import json
import base64
import io
import sqlite3
from aiohttp import web
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"


def _extra_col(row, extra_base: int, extra_cols: str, name: str):
    """从 SELECT 结果里取 life_stage / relation_detail 这类可选追加列。

    [2026-10-05] 追加列是否存在于 SELECT 里取决于旧库有没有跑迁移，
    所以按 extra_cols 的实际顺序推下标，取不到就返回空串 —— 不能因为
    「列不存在」让整个家庭成员列表接口报错。
    """
    if not extra_cols or name not in extra_cols:
        return ""
    # extra_cols 形如 ", life_stage, relation_detail"，顺序即下标顺序
    names = [c.strip().lstrip(",").strip() for c in extra_cols.split(",") if c.strip()]
    if name not in names:
        return ""
    idx = extra_base + names.index(name)
    return row[idx] if len(row) > idx and row[idx] is not None else ""

def evaluate_health_status_from_data(member_id, cursor):
    """根据健康数据评估成员健康状态"""
    try:
        # 获取成员基本信息（身高、年龄等）
        cursor.execute("""
            SELECT height, weight, age, gender
            FROM family_members
            WHERE id = ?
        """, (member_id,))
        member_info = cursor.fetchone()
        
        if not member_info:
            return '健康'
        
        height_m = member_info[0] / 100.0 if member_info[0] else 0  # 身高转换为米
        current_weight = member_info[1]
        age = member_info[2]
        gender = member_info[3]
        
        # 获取最近的健康数据
        cursor.execute("""
            SELECT data_type, value_main, value_extra, timestamp 
            FROM health_data 
            WHERE member_id = ? 
            ORDER BY timestamp DESC LIMIT 10
        """, (member_id,))
        health_rows = cursor.fetchall()
        
        if not health_rows:
            return '健康'  # 如果没有健康数据，默认为健康
        
        # 分析健康数据
        issues = []
        latest_values = {}  # 存储最新的各项数值
        
        for row in health_rows:
            data_type = row[0]
            value_main = row[1]
            value_extra = row[2]
            
            if data_type == 'blood_pressure':
                # 解析血压数据
                import json
                try:
                    extra = json.loads(value_extra) if value_extra else {}
                    systolic = extra.get('systolic', value_main)
                    diastolic = extra.get('diastolic', 0)
                    
                    # 存储最新血压值用于后续判断
                    latest_values['systolic'] = systolic
                    latest_values['diastolic'] = diastolic
                    
                    # 根据年龄调整血压标准
                    if age:
                        if age < 18:  # 儿童血压标准
                            if systolic and (systolic > 120 or systolic < 90):
                                issues.append('血压异常')
                            if diastolic and (diastolic > 80 or diastolic < 50):
                                issues.append('血压异常')
                        elif age >= 60:  # 老年人血压标准稍宽松
                            if systolic and (systolic > 150 or systolic < 90):
                                issues.append('血压异常')
                            if diastolic and (diastolic > 90 or diastolic < 60):
                                issues.append('血压异常')
                        else:  # 成年人标准
                            if systolic and (systolic > 140 or systolic < 90):
                                issues.append('血压异常')
                            if diastolic and (diastolic > 90 or diastolic < 60):
                                issues.append('血压异常')
                    else:  # 默认成年人标准
                        if systolic and (systolic > 140 or systolic < 90):
                            issues.append('血压异常')
                        if diastolic and (diastolic > 90 or diastolic < 60):
                            issues.append('血压异常')
                except:
                    pass
            elif data_type == 'blood_oxygen':
                # 血氧异常判断
                if value_main and value_main < 95:
                    issues.append('血氧偏低')
            elif data_type == 'heart_rate':
                # 存储最新心率值用于后续判断
                latest_values['heart_rate'] = value_main
                
                # 根据年龄调整心率标准
                if age:
                    if age < 18:  # 儿童心率标准
                        if value_main and (value_main > 100 or value_main < 70):
                            issues.append('心率异常')
                    elif age >= 60:  # 老年人心率标准
                        if value_main and (value_main > 90 or value_main < 50):
                            issues.append('心率异常')
                    else:  # 成年人标准
                        if value_main and (value_main > 100 or value_main < 60):
                            issues.append('心率异常')
                else:  # 默认成年人标准
                    if value_main and (value_main > 100 or value_main < 60):
                        issues.append('心率异常')
            elif data_type == 'body_temperature':
                # 存储最新体温值用于后续判断
                latest_values['temperature'] = value_main
                
                # 体温异常判断
                if value_main and (float(value_main) > 37.5 or float(value_main) < 36.0):
                    issues.append('体温异常')
            elif data_type == 'weight':
                # BMI计算和评估
                if height_m > 0 and current_weight:
                    bmi = current_weight / (height_m * height_m)
                    latest_values['bmi'] = bmi
                    
                    # 存储BMI值用于后续判断
                    latest_values['bmi'] = bmi
                    
                    # 根据年龄和性别调整BMI标准
                    if age:
                        if age < 18:  # 儿童BMI标准
                            # 儿童BMI标准更复杂，这里简化处理
                            if bmi > 25 or bmi < 13:
                                issues.append('BMI异常')
                        else:  # 成年人BMI标准
                            if bmi > 28:  # 肥胖
                                issues.append('BMI异常')
                            elif bmi < 18.5:  # 偏瘦
                                issues.append('BMI异常')
                    else:  # 默认成年人BMI标准
                        if bmi > 28:
                            issues.append('BMI异常')
                        elif bmi < 18.5:
                            issues.append('BMI异常')
        
        # 根据问题数量和类型确定健康状态
        if len(issues) == 0:
            return '健康'
        elif len(issues) <= 2:
            # 检查是否需要建议就医（严重异常）
            if '体温异常' in issues and latest_values.get('temperature') and float(latest_values['temperature']) > 38.5:  # 高烧
                return '建议就医'
            elif '血压异常' in issues and latest_values.get('systolic') and latest_values['systolic'] > 180:  # 严重高血压
                return '建议就医'
            elif '心率异常' in issues and latest_values.get('heart_rate') and (latest_values['heart_rate'] > 120 or latest_values['heart_rate'] < 40):  # 严重心率异常
                return '建议就医'
            elif '血压异常' in issues and age and age >= 60:  # 老年人血压异常需要特别关注
                return '建议就医'
            return '亚健康'
        elif '血压异常' in issues or 'BMI异常' in issues or '体温异常' in issues:
            # 严重异常情况
            if '体温异常' in issues and latest_values.get('temperature') and float(latest_values['temperature']) > 38.5:  # 高烧
                return '建议就医'
            elif '血压异常' in issues and latest_values.get('systolic') and latest_values['systolic'] > 180:  # 严重高血压
                return '建议就医'
            elif '心率异常' in issues and latest_values.get('heart_rate') and (latest_values['heart_rate'] > 120 or latest_values['heart_rate'] < 40):  # 严重心率异常
                return '建议就医'
            return '需关注'
        else:
            # 检查是否需要加强锻炼（BMI相关）
            if 'BMI异常' in issues:
                bmi = latest_values.get('bmi', 0)
                if bmi > 24:  # 超重
                    return '加强锻炼'
            return '注意饮食'
            
    except Exception as e:
        print(f"评估健康状态时出错: {e}")
        return '健康'  # 出错时返回默认状态


async def list_family_members(request):
    """列出所有家庭成员（控制台格式）"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 获取家庭成员
            # [2026-10-05] 补 life_stage / relation_detail 两列（地点簿方案）：
            #   life_stage      人生阶段，驱动「我上学了/我上班了」的话术
            #   relation_detail 血缘路径（自由文本），解决「小花朵的爷爷」这类歧义
            # 用「表里有哪些列」来决定 SELECT，避免旧库（没跑迁移）直接报错。
            cursor.execute("PRAGMA table_info(family_members)")
            fam_cols = {r[1] for r in cursor.fetchall()}
            extra_cols = ""
            if "life_stage" in fam_cols:
                extra_cols += ", life_stage"
            if "relation_detail" in fam_cols:
                extra_cols += ", relation_detail"

            cursor.execute(f"""
                SELECT id, name, relation, avatar, age, gender, birthday,
                       height, weight, occupation, position, notes, health_enabled,
                       health_status, phone, created_at, updated_at, voice_print_vector, nickname
                       {extra_cols}
                FROM family_members
            """)
            members_rows = cursor.fetchall()

            # 追加列的起始下标（基础 SELECT 固定 19 列，索引 0..18）
            extra_base = 19

            # 构建成员列表
            members = []
            for row in members_rows:
                member_id = row[0]
                # SELECT 列序：16=updated_at, 17=voice_print_vector, 18=nickname
                # 原代码误读 row[16]（updated_at）当声纹向量，已修正（主人 2026-09-09）
                voice_print_vector = row[17] if len(row) > 17 else None
            
                # 获取该成员绑定的健康设备
                cursor.execute("""
                    SELECT device_id FROM health_device_bindings
                    WHERE member_id = ?
                """, (member_id,))
                devices_rows = cursor.fetchall()
                devices = [d[0] for d in devices_rows]
            
                # 根据健康数据动态评估健康状态
                dynamic_health_status = evaluate_health_status_from_data(member_id, cursor)
            
                # 构建控制台期望的数据格式
                members.append({
                    'id': row[0],
                    'name': row[1],
                    'role': row[2],
                    'relation': row[2],
                    'avatar': row[3],
                    'age': row[4],
                    'gender': row[5],
                    'birthday': row[6],
                    'height': row[7],
                    'weight': row[8],
                    'occupation': row[9],
                    'position': row[10],
                    'notes': row[11],
                    'health_enabled': bool(row[12]),
                    'health_status': dynamic_health_status,
                    'phone': row[14],
                    'devices': devices,
                    'created_at': row[15],
                    'updated_at': row[16],
                    'nickname': row[18] if len(row) > 18 else '',
                    'life_stage': _extra_col(row, extra_base, extra_cols, "life_stage"),
                    'relation_detail': _extra_col(row, extra_base, extra_cols, "relation_detail"),
                    'voice_print_vector': voice_print_vector  # ⚠️ 必须返回，否则前端认为无声纹
                })
        
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'members': members
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def add_family_member(request):
    """添加家庭成员"""
    try:
        data = await request.json()
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()

            # 处理声纹数据：如果有 base64 音频，提取特征向量
            voice_print_vector = None
            voiceprint_data = data.get('voiceprint', '')

            if voiceprint_data and voiceprint_data.startswith('data:audio'):
                # 从 base64 提取音频数据
                if ',' in voiceprint_data:
                    base64_audio = voiceprint_data.split(',')[1]
                else:
                    base64_audio = voiceprint_data

                audio_bytes = base64.b64decode(base64_audio)

                # 调用声纹识别提取特征
                try:
                    from core.utils.voiceprint_provider import VoiceprintProvider
                    vp = VoiceprintProvider({})
                    if vp.enabled:
                        # audio_bytes 是 WAV 格式，直接使用
                        embedding = vp._extract_embedding(audio_bytes)
                        if embedding is not None:
                            voice_print_vector = json.dumps(embedding.tolist())
                            print(f"[声纹录入] 成功提取特征向量，维度: {len(embedding)}")
                except Exception as e:
                    print(f"[声纹录入] 特征提取失败: {e}")

            # [2026-10-05] life_stage / relation_detail 是迁移新增的列，
            # 旧库（没跑迁移）不能直接 INSERT 不存在的列，否则整个添加成员会失败。
            # 这里先探测表结构再决定要不要带上。
            cursor.execute("PRAGMA table_info(family_members)")
            fam_cols = {r[1] for r in cursor.fetchall()}
            add_cols = ""
            add_vals = ""
            if "life_stage" in fam_cols:
                add_cols += ", life_stage"
                add_vals += ", ?"
            if "relation_detail" in fam_cols:
                add_cols += ", relation_detail"
                add_vals += ", ?"

            cursor.execute(f"""
                INSERT INTO family_members (name, nickname, relation, avatar, age, gender, birthday,
                                            height, weight, occupation, position, notes, health_enabled, phone, voice_print_vector
                                            {add_cols})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{add_vals})
            """, (
                data.get('name'),
                data.get('nickname') or '',
                data.get('relation') or data.get('role'),
                data.get('avatar'),
                data.get('age'),
                data.get('gender'),
                data.get('birthday'),
                data.get('height'),
                data.get('weight'),
                data.get('occupation'),
                data.get('position'),
                data.get('notes'),
                int(data.get('health_enabled', True)),
                data.get('phone', ''),
                voice_print_vector,
                *[data.get(c) or '' for c in ('life_stage', 'relation_detail')
                  if c in fam_cols]
            ))

            member_id = cursor.lastrowid

            # 如果提供了健康设备绑定，创建绑定关系
            devices = data.get('devices', [])
            for device_id in devices:
                cursor.execute("""
                    INSERT INTO health_device_bindings (member_id, device_id, binding_type, auto_associate)
                    VALUES (?, ?, 'primary', 1)
                """, (member_id, device_id))

            conn.commit()
        finally:
            conn.close()

        return web.json_response({
            'success': True,
            'id': member_id,
            'voice_print_vector': voice_print_vector,
            'message': f'成员 {data.get("name")} 已添加'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def update_family_member(request):
    """更新家庭成员"""
    try:
        member_id = int(request.match_info.get('member_id'))
        print(f"[声纹调试] PUT 请求到达 update_family_member, member_id={member_id}")
        data = await request.json()

        # 处理声纹数据：如果有 base64 音频，提取特征向量
        voice_print_vector = None
        voiceprint_data = data.get('voiceprint', '')
        print(f"[声纹调试] PUT /api/family/members/{member_id}, voiceprint长度={len(voiceprint_data)}, 前缀={voiceprint_data[:50] if voiceprint_data else '(空)'}")

        if voiceprint_data and voiceprint_data.startswith('data:audio'):
            # 从 base64 提取音频数据
            if ',' in voiceprint_data:
                base64_audio = voiceprint_data.split(',')[1]
            else:
                base64_audio = voiceprint_data

            audio_bytes = base64.b64decode(base64_audio)

            # 调用声纹识别提取特征
            try:
                from core.utils.voiceprint_provider import VoiceprintProvider
                vp = VoiceprintProvider({})
                if vp.enabled:
                    embedding = vp._extract_embedding(audio_bytes)
                    if embedding is not None:
                        voice_print_vector = json.dumps(embedding.tolist())
                        print(f"[声纹更新] 成功提取特征向量，维度: {len(embedding)}")
            except Exception as e:
                print(f"[声纹更新] 特征提取失败: {e}")

        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()

            # [2026-10-05] life_stage / relation_detail 是迁移新增的列，
            # 旧库不能 UPDATE 不存在的列。探测后再决定带不带。
            cursor.execute("PRAGMA table_info(family_members)")
            fam_cols = {r[1] for r in cursor.fetchall()}
            set_extra = ""
            extra_vals = []
            if "life_stage" in fam_cols:
                set_extra += ", life_stage=?"
                extra_vals.append(data.get('life_stage') or '')
            if "relation_detail" in fam_cols:
                set_extra += ", relation_detail=?"
                extra_vals.append(data.get('relation_detail') or '')

            if voice_print_vector:
                # 有声纹数据，更新声纹向量
                cursor.execute(f"""
                    UPDATE family_members
                    SET name=?, nickname=?, relation=?, avatar=?, age=?, gender=?, birthday=?,
                        height=?, weight=?, occupation=?, position=?, notes=?, health_enabled=?, phone=?, voice_print_vector=?, updated_at=?{set_extra}
                    WHERE id=?
                """, (
                    data.get('name'),
                    data.get('nickname') or '',
                    data.get('relation') or data.get('role'),
                    data.get('avatar'),
                    data.get('age'),
                    data.get('gender'),
                    data.get('birthday'),
                    data.get('height'),
                    data.get('weight'),
                    data.get('occupation'),
                    data.get('position'),
                    data.get('notes'),
                    int(data.get('health_enabled', True)),
                    data.get('phone', ''),
                    voice_print_vector,
                    datetime.now().isoformat(),
                    *extra_vals,
                    member_id
                ))
            else:
                # 没有声纹数据，不更新声纹向量字段
                cursor.execute(f"""
                    UPDATE family_members
                    SET name=?, nickname=?, relation=?, avatar=?, age=?, gender=?, birthday=?,
                        height=?, weight=?, occupation=?, position=?, notes=?, health_enabled=?, phone=?, updated_at=?{set_extra}
                    WHERE id=?
                """, (
                    data.get('name'),
                    data.get('nickname') or '',
                    data.get('relation') or data.get('role'),
                    data.get('avatar'),
                    data.get('age'),
                    data.get('gender'),
                    data.get('birthday'),
                    data.get('height'),
                    data.get('weight'),
                    data.get('occupation'),
                    data.get('position'),
                    data.get('notes'),
                    int(data.get('health_enabled', True)),
                    data.get('phone', ''),
                    datetime.now().isoformat(),
                    *extra_vals,
                    member_id
                ))

            conn.commit()
        finally:
            conn.close()

        return web.json_response({
            'success': True,
            'voice_print_vector': voice_print_vector,
            'message': f'成员 {member_id} 已更新'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def delete_family_member(request):
    """删除家庭成员"""
    try:
        member_id = int(request.match_info.get('member_id'))
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 删除设备绑定
            cursor.execute("DELETE FROM health_device_bindings WHERE member_id=?", (member_id,))
        
            # 删除成员
            cursor.execute("DELETE FROM family_members WHERE id=?", (member_id,))
        
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'message': f'成员 {member_id} 已删除'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def delete_member_voiceprint(request):
    """删除成员的声纹（清空特征向量）

    [2026-10-14] 主人报「删除声纹」名不副实：前端原本只清隐藏字段，保存时
    `update_family_member` 走「没有声纹数据就不更新向量」分支 → 库里的
    voice_print_vector 原样保留，重开弹窗又显示「已录入」。
    这里给一个明确的删除接口，只清声纹、不动其他字段。
    """
    try:
        member_id = int(request.match_info.get('member_id'))

        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE family_members
                SET voice_print_vector = NULL, updated_at = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), member_id))
            changed = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

        # 成员不存在时 rowcount=0：不算错误，但如实告诉调用方「没删到」
        return web.json_response({
            'success': True,
            'deleted': bool(changed),
            'message': f'成员 {member_id} 的声纹已删除' if changed else f'成员 {member_id} 不存在'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })


async def bind_health_device(request):
    """绑定健康设备到成员"""
    try:
        member_id = int(request.match_info.get('member_id'))
        data = await request.json()
        device_id = data.get('device_id')
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 创建绑定
            cursor.execute("""
                INSERT OR REPLACE INTO health_device_bindings 
                (member_id, device_id, binding_type, auto_associate)
                VALUES (?, ?, 'primary', 1)
            """, (member_id, device_id))
        
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'device_id': device_id,
            'message': f'设备 {device_id} 已绑定到成员'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def unbind_health_device(request):
    """解绑健康设备"""
    try:
        member_id = int(request.match_info.get('member_id'))
        device_id = request.query.get('device_id')
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            cursor.execute("""
                DELETE FROM health_device_bindings 
                WHERE member_id=? AND device_id=?
            """, (member_id, device_id))
        
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'message': f'设备 {device_id} 已解绑'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })


async def get_member_schedules(request):
    """获取成员日程（当日+次日）"""
    try:
        member_id = int(request.match_info.get('member_id'))
        from datetime import date, timedelta
        today = str(date.today())
        tomorrow = str(date.today() + timedelta(days=1))
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            cursor.execute("""
                SELECT id, title, date, time, end_time, source, notes
                FROM member_schedules
                WHERE member_id = ? AND date IN (?, ?)
                ORDER BY date, time
            """, (member_id, today, tomorrow))
        
            schedules = []
            for row in cursor.fetchall():
                schedules.append({
                    'id': row[0],
                    'title': row[1],
                    'date': row[2],
                    'time': row[3],
                    'end_time': row[4],
                    'source': row[5],
                    'notes': row[6]
                })
        
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'schedules': schedules,
            'dates': [today, tomorrow]
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })


async def add_member_schedule(request):
    """添加成员日程"""
    try:
        member_id = int(request.match_info.get('member_id'))
        data = await request.json()
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            cursor.execute("""
                INSERT INTO member_schedules (member_id, title, date, time, end_time, source, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                member_id,
                data.get('title'),
                data.get('date'),
                data.get('time'),
                data.get('end_time'),
                data.get('source', 'manual'),
                data.get('notes')
            ))
        
            schedule_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'id': schedule_id,
            'message': '日程已添加'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })


async def delete_member_schedule(request):
    """删除成员日程"""
    try:
        schedule_id = int(request.match_info.get('schedule_id'))
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM member_schedules WHERE id=?", (schedule_id,))
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'message': '日程已删除'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })


async def get_member_health_records(request):
    """获取成员健康记录（用于长期曲线，从 health_data 读取）"""
    try:
        member_id = int(request.match_info.get('member_id'))
        days = int(request.query.get('days', 30))
        
        from datetime import date, timedelta
        start_date = str(date.today() - timedelta(days=days))
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 从 health_data 按日期聚合
            cursor.execute("""
                SELECT DATE(timestamp) as record_date, data_type, value_main, value_extra
                FROM health_data
                WHERE member_id = ? AND timestamp >= ? AND is_baseline = 0
                ORDER BY record_date, data_type
            """, (member_id, start_date))
        
            # 按日期聚合
            records_map = {}
            for row in cursor.fetchall():
                record_date, data_type, value_main, value_extra = row
                if record_date not in records_map:
                    records_map[record_date] = {
                        'date': record_date,
                        'heart_rate': None,
                        'sleep_hours': None,
                        'steps': None,
                        'weight': None,
                        'blood_pressure_systolic': None,
                        'blood_pressure_diastolic': None,
                        'blood_oxygen': None,
                        'blood_glucose': None,
                        'temperature': None,
                        'notes': None
                    }
            
                if data_type == 'heart_rate':
                    records_map[record_date]['heart_rate'] = value_main
                elif data_type == 'weight':
                    records_map[record_date]['weight'] = value_main
                elif data_type == 'blood_pressure' and value_extra:
                    import json
                    extra = json.loads(value_extra)
                    records_map[record_date]['blood_pressure_systolic'] = extra.get('systolic', value_main)
                    records_map[record_date]['blood_pressure_diastolic'] = extra.get('diastolic')
                elif data_type == 'blood_oxygen':
                    records_map[record_date]['blood_oxygen'] = value_main
                elif data_type == 'glucose':
                    records_map[record_date]['blood_glucose'] = value_main
                elif data_type in ('body_temperature', 'temperature'):
                    records_map[record_date]['temperature'] = value_main
        
            records = sorted(records_map.values(), key=lambda x: x['date'])
        
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'records': records,
            'days': days
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })


async def update_member_health_status(request):
    """更新成员健康状态"""
    try:
        member_id = int(request.match_info.get('member_id'))
        data = await request.json()
        health_status = data.get('health_status', '健康')
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE family_members SET health_status = ?, updated_at = ?
                WHERE id = ?
            """, (health_status, datetime.now().isoformat(), member_id))
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'message': f'健康状态已更新为 {health_status}'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })