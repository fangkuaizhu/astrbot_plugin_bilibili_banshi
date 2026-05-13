"""
B站搬石插件 - 随机从B站搜索视频并分享到群
v0.6.0：群组独立模式 - 每群独立开关/关键词/历史/推送模式
"""

import asyncio
import random
import re
import json
import time
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, StarTools, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain


# ========== 工具函数 ==========

def _parse_duration(text: str) -> int:
    if not text:
        return 0
    try:
        parts = text.replace(":", "：").split("：")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return int(text)
    except:
        return 0

def _format_count(num: int) -> str:
    if num >= 100000000:
        return f"{num / 100000000:.1f}亿"
    elif num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)

def _parse_play(text: str) -> int:
    if isinstance(text, int):
        return text
    try:
        t = str(text).strip()
        mult = 1
        if "万" in t:
            t = t.replace("万", "")
            mult = 10000
        elif "亿" in t:
            t = t.replace("亿", "")
            mult = 100000000
        nums = re.findall(r"\d+\.?\d*", t)
        return int(float(nums[0]) * mult) if nums else 0
    except:
        return 0


@register(
    "astrbot_plugin_bilibili_banshi",
    "Hanako",
    "B站搬石 - 群组独立版，每群独立开关/关键词/历史",
    "0.6.0"
)
class BilibiliBanshiPlugin(Star):
    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context)

        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_bilibili_banshi"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.config_path = self.data_dir / "config.json"

        # AstrBotConfig（来自 _conf_schema.json，作为全局默认值）
        self.astrbot_cfg = config

        # 加载本地配置
        self._config = self._load_config()
        self._ensure_group_configs()

        # 运行状态
        self.running = False           # 定时任务是否在跑
        self.task: Optional[asyncio.Task] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._shutdown = False

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

        # 注册 WebUI 设置页 API
        PLUGIN_NAME = "astrbot_plugin_bilibili_banshi"
        try:
            context.register_web_api(
                f"/{PLUGIN_NAME}/groups",
                self.api_get_groups,
                ["GET"],
                "获取所有群的搬石配置",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/groups/<group_id>/config",
                self.api_update_group_config,
                ["POST"],
                "更新指定群的搬石配置",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/groups/<group_id>/reset",
                self.api_reset_group_config,
                ["POST"],
                "重置指定群配置为默认值",
            )
            logger.info("注册搬石 WebUI API 成功")
        except Exception as e:
            logger.warning(f"注册搬石 WebUI API 失败: {e}")

        logger.info(f"B站搬石 v4 已加载，数据目录: {self.data_dir}")

    # ========== 配置管理 ==========

    def _default_group_config(self) -> Dict[str, Any]:
        """新群的默认配置"""
        defaults = {
            "enabled": False,         # 独立开关
            "keywords": [],           # 独立关键词（空=继承全局）
            "max_duration": 0,       # 0=不限时长
            "max_pages": 3,           # 独立搜索页数
            "sent_bvids": [],         # 独立已发记录
            "scan_mode": "interval", # interval=间隔推送, scheduled=定时推送
            "scan_interval": 300,     # 间隔推送的间隔（秒）
            "push_times": ["08:00","12:00","18:00"],  # 定时推送的时间点（HH:MM）
            "last_scan_time": 0,      # 上次扫描时间戳
        }
        # 从 AstrBotConfig 获取全局默认值（schema 键名：default_keywords / default_max_pages）
        if self.astrbot_cfg:
            kw = self.astrbot_cfg.get("default_keywords", [])
            if kw:
                defaults["keywords"] = kw.copy()
            defaults["max_pages"] = self.astrbot_cfg.get("default_max_pages", 3)
        if not defaults["keywords"]:
            defaults["keywords"] = ["咕咕嘎嘎", "凑企鹅", "企鹅", "艾特", "抽象"]
        return defaults

    def _ensure_group_configs(self):
        """确保所有已绑群都有配置，新群自动用默认值"""
        if "group_configs" not in self._config:
            self._config["group_configs"] = {}
        groups = self._config.get("bound_groups", {})
        default = self._default_group_config()
        for gid in groups:
            gc = self._config["group_configs"].get(gid)
            if gc is None:
                self._config["group_configs"][gid] = default.copy()
            else:
                # 迁移旧配置：补充新字段
                for key in default:
                    if key not in gc:
                        gc[key] = default[key]

    def _get_group_cfg(self, group_id: str) -> Dict[str, Any]:
        """获取某群的配置（不存在则创建）"""
        if "group_configs" not in self._config:
            self._config["group_configs"] = {}
        if group_id not in self._config["group_configs"]:
            self._config["group_configs"][group_id] = self._default_group_config()
        return self._config["group_configs"][group_id]

    def _get_group_keywords(self, group_id: str) -> List[str]:
        """获取某群的关键词（空则用默认列表）"""
        g = self._get_group_cfg(group_id)
        if g.get("keywords"):
            return g["keywords"]
        # 返回一个随机默认词表（不保存到配置，每次调用随机抽）
        default_kw = ["咕咕嘎嘎", "凑企鹅", "企鹅", "艾特", "抽象"]
        return default_kw

    def _get_sent_set(self, group_id: str) -> set:
        """获取某群的已发送 BV 集合"""
        g = self._get_group_cfg(group_id)
        return set(g.get("sent_bvids", []))

    def _add_sent(self, group_id: str, bvid: str):
        """向某群的已发送记录添加 BV"""
        g = self._get_group_cfg(group_id)
        if "sent_bvids" not in g:
            g["sent_bvids"] = []
        if bvid not in g["sent_bvids"]:
            g["sent_bvids"].append(bvid)
        # 最多保留 500 条
        if len(g["sent_bvids"]) > 500:
            g["sent_bvids"] = g["sent_bvids"][-500:]

    def _load_config(self) -> Dict[str, Any]:
        cfg = {
            "bound_groups": {},
            "group_configs": {},
        }
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    cfg.update(saved)
            except Exception as e:
                logger.error(f"读取配置文件失败: {e}")

        # === v3→v4 迁移：将全局配置转为每群配置 ===
        old_cfg = cfg.copy()
        has_old_style = any(k in old_cfg for k in ["search_keywords", "max_duration", "max_pages", "auto_start"])
        if has_old_style and not old_cfg.get("group_configs"):
            logger.info("检测到 v3 旧配置，正在迁移为群组独立配置...")
            old_kw = old_cfg.get("search_keywords", ["咕咕嘎嘎", "凑企鹅", "企鹅", "艾特", "抽象"])
            old_dur = old_cfg.get("max_duration", 600)
            old_pages = old_cfg.get("max_pages", 3)
            old_auto = old_cfg.get("auto_start", False)

            # 读取旧的历史记录文件
            old_sent = []
            history_path = self.data_dir / "history.json"
            if history_path.exists():
                try:
                    with open(history_path, "r", encoding="utf-8") as f:
                        old_sent = json.load(f)
                except Exception:
                    pass

            # 为每个已绑群创建配置
            for gid in old_cfg.get("bound_groups", {}):
                cfg.setdefault("group_configs", {})[gid] = {
                    "enabled": old_auto,
                    "keywords": old_kw.copy(),
                    "max_duration": old_dur,
                    "max_pages": old_pages,
                    "sent_bvids": old_sent.copy(),
                }

            # 清理旧字段
            for k in ["search_keywords", "max_duration", "max_pages", "auto_start", "send_to_all", "blacklist_groups", "sent_bvids"]:
                cfg.pop(k, None)

            # 立即写回磁盘
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                # 删除旧的 history.json
                if history_path.exists():
                    history_path.unlink()
            except Exception as e:
                logger.error(f"迁移配置写入失败: {e}")

            logger.info(f"迁移完成，已为 {len(cfg.get('bound_groups',{}))} 个群创建独立配置")

        return cfg

    async def _save_config(self):
        try:
            temp = self.config_path.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            if self.config_path.exists():
                self.config_path.unlink()
            temp.rename(self.config_path)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    # ========== 生命周期 ==========

    async def initialize(self):
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        self.session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)

        # 检查是否有任何群开启了 enabled，有则启动定时任务
        any_enabled = False
        for gid, gc in self._config.get("group_configs", {}).items():
            if gc.get("enabled", False):
                any_enabled = True
                break

        if any_enabled:
            self.running = True
            self._shutdown = False
            self.task = asyncio.create_task(self._timer_task())
            logger.info(f"B站搬石定时任务已启动（{sum(1 for g in self._config.get('group_configs',{}).values() if g.get('enabled'))} 个群已开启）")
        else:
            logger.info("B站搬石已加载，未开启任何群（使用 /banshi on 开启）")

    async def terminate(self):
        self._shutdown = True
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("定时任务 5 秒内未停止")
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as e:
                logger.error(f"停止定时任务时出错: {e}")
            self.task = None
        if self.session and not self.session.closed:
            await self.session.close()

    # ========== 自动记录群 ==========

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            umo = event.unified_msg_origin
            group_id = str(event.message_obj.group_id)
            if not group_id or not umo:
                return
            if "bound_groups" not in self._config:
                self._config["bound_groups"] = {}
            if group_id not in self._config["bound_groups"]:
                self._config["bound_groups"][group_id] = umo
                self._ensure_group_configs()
                await self._save_config()
                logger.info(f"自动记录新群: {group_id}")
        except Exception as e:
            logger.error(f"自动记录群失败: {e}")

    # ========== B站API ==========

    async def _search_videos(self, keyword: str, sent_set: set, max_dur: int, max_pages: int) -> List[Dict[str, Any]]:
        if not self.session:
            return []

        videos = []
        for page in range(1, max_pages + 1):
            if self._shutdown or not self.running:
                return videos

            try:
                url = f"https://api.bilibili.com/x/web-interface/search/all/v2?keyword={keyword}&page={page}"
                async with self.session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()

                if data.get("code") != 0:
                    break

                video_results = None
                for item in data.get("data", {}).get("result", []):
                    if item.get("result_type") == "video":
                        video_results = item.get("data", [])
                        break

                if not video_results:
                    break

                for v in video_results:
                    if self._shutdown or not self.running:
                        return videos
                    try:
                        title = re.sub(r"<[^>]+>", "", v.get("title", ""))
                        bvid = v.get("bvid", "")
                        if not bvid or bvid in sent_set:
                            continue
                        dur_text = v.get("duration", "")
                        dur_sec = _parse_duration(dur_text)
                        # max_dur=0 表示不限时长
                        if max_dur > 0 and dur_sec > max_dur:
                            continue
                        videos.append({
                            "title": title,
                            "bvid": bvid,
                            "url": f"https://www.bilibili.com/video/{bvid}",
                            "author": v.get("author", "未知"),
                            "duration": dur_text,
                            "play": _format_count(_parse_play(v.get("play", "0"))),
                            "keyword": keyword,
                        })
                    except Exception:
                        continue

                if page < max_pages:
                    await asyncio.sleep(random.uniform(0.3, 0.8))

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(f"搜索 '{keyword}' 第{page}页超时")
                continue
            except Exception as e:
                logger.error(f"搜索 '{keyword}' 第{page}页出错: {e}")
                break

        return videos

    async def _select_video(self, group_id: str) -> Optional[Dict[str, Any]]:
        keywords = self._get_group_keywords(group_id)
        sent_set = self._get_sent_set(group_id)
        gc = self._get_group_cfg(group_id)
        max_dur = gc.get("max_duration", 600)
        max_pages = gc.get("max_pages", 3)

        max_attempts = 15
        for _ in range(max_attempts):
            if self._shutdown or not self.running:
                return None

            keyword = random.choice(keywords) if keywords else "有趣"
            pages = random.randint(1, max_pages)
            videos = await self._search_videos(keyword, sent_set, max_dur, pages)

            if self._shutdown or not self.running:
                return None

            if not videos:
                continue

            selected = random.choice(videos)
            logger.info(f"[群{group_id}] 选中: {selected['title']} (BV: {selected['bvid']})")
            return selected

        logger.error(f"[群{group_id}] 尝试 {max_attempts} 次后未找到合适视频")
        return None

    async def _get_video_detail(self, bvid: str) -> Optional[Dict[str, Any]]:
        if not self.session:
            return None
        try:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            if data.get("code") != 0:
                return None
            detail = data.get("data", {})
            return {
                "owner": detail.get("owner", {}).get("name", ""),
                "stat": detail.get("stat", {}),
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"获取视频详情失败 {bvid}: {e}")
            return None

    def _build_card(self, video: Dict[str, Any], detail: Optional[Dict[str, Any]] = None) -> str:
        title = video.get("title", "未知标题")
        author = video.get("author", "未知UP主")
        duration = video.get("duration", "?")
        play = video.get("play", "?")
        url = video.get("url", "")
        parts = [
            f"🎬 【B站搬石】",
            f"",
            f"📌 {title}",
            f"",
            f"👤 UP主：{author}",
            f"⏱️ 时长：{duration}",
            f"▶️ 播放：{play}",
        ]
        if detail:
            stat = detail.get("stat", {})
            like = stat.get("like", 0)
            coin = stat.get("coin", 0)
            share = stat.get("share", 0)
            parts.extend([
                f"👍 点赞：{_format_count(int(like))}",
                f"🎯 投币：{_format_count(int(coin))}",
                f"📤 分享：{_format_count(int(share))}",
            ])
        parts.extend([f"", f"🔗 {url}"])
        return "\n".join(parts)

    async def _run_once(self, group_id: str, umo: str):
        """对指定群执行一次搬石"""
        video = await self._select_video(group_id)
        if not video:
            return None

        bvid = video["bvid"]
        self._add_sent(group_id, bvid)
        await self._save_config()

        detail = await self._get_video_detail(bvid)
        card = self._build_card(video, detail)

        chain = MessageChain([Plain(card)])
        try:
            await self.context.send_message(umo, chain)
            logger.info(f"[群{group_id}] 已发送: {bvid}")
            return bvid
        except Exception as e:
            logger.error(f"[群{group_id}] 发送失败: {e}")
            return None

    async def _timer_task(self):
        """定时任务：短循环检测每群是否满足触发条件"""
        CHECK_INTERVAL = 30  # 每30秒检查一次
        while True:
            if self._shutdown or not self.running:
                logger.info("定时任务已停止")
                break

            try:
                await asyncio.wait_for(asyncio.sleep(CHECK_INTERVAL), timeout=CHECK_INTERVAL + 1)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                logger.info("定时任务收到取消信号，退出")
                break

            if self._shutdown or not self.running:
                logger.info("定时任务已停止")
                break

            now_ts = time.time()
            now_hm = datetime.datetime.now().strftime("%H:%M")
            triggered = False

            for gid, gc in list(self._config.get("group_configs", {}).items()):
                if not gc.get("enabled", False):
                    continue
                umo = self._config.get("bound_groups", {}).get(gid)
                if not umo:
                    continue

                mode = gc.get("scan_mode", "interval")
                last = gc.get("last_scan_time", 0)
                should_scan = False

                if mode == "interval":
                    interval = gc.get("scan_interval", 300)
                    if now_ts - last >= interval:
                        should_scan = True
                elif mode == "scheduled":
                    push_times = gc.get("push_times", [])
                    if now_hm in push_times:
                        # 同一分钟内不重复触发
                        last_hm = datetime.datetime.fromtimestamp(last).strftime("%H:%M")
                        if now_hm != last_hm:
                            should_scan = True

                if should_scan:
                    triggered = True
                    gc["last_scan_time"] = now_ts
                    await self._save_config()
                    try:
                        await self._run_once(gid, umo)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(f"[群{gid}] 定时搬石异常: {e}")

            if not triggered:
                continue

    # ========== 主指令分发 ==========

    @filter.command("banshi")
    async def banshi(self, event: AstrMessageEvent):
        """B站搬石 - 群组独立版"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result(
                "🎬 B站搬石插件 v4.0（群组独立版）\n"
                "每群的开关、关键词、已发记录均独立。\n"
                "用法：\n"
                "  /banshi on        — 在本群开启搬石\n"
                "  /banshi off       — 在本群关闭搬石\n"
                "  /banshi now       — 本群立即搬一次\n"
                "  /banshi list      — 本群当前状态\n"
                "  /banshi kw        — 本群关键词列表\n"
                "  /banshi kw add <词>  — 本群添加关键词\n"
                "  /banshi kw del <词>  — 本群删除关键词\n"
                "  /banshi maxdur <秒>  — 本群视频最大时长\n"
                "  /banshi interval <秒>  — 本群间隔推送间隔\n"
                "  /banshi mode interval  — 切换为间隔推送模式\n"
                "  /banshi mode scheduled — 切换为定时推送模式\n"
                "  /banshi addtime <HH:MM> — 增加一个定时推送时间点\n"
                "  /banshi deltime <HH:MM> — 删除一个定时推送时间点\n"
                "  /banshi reset     — 清空本群已发记录"
            )
            return

        msg = event.message_str.strip()
        full_cmds = {
            "banshi on": self._cmd_on,
            "banshi off": self._cmd_off,
            "banshi now": self._cmd_now,
            "banshi list": self._cmd_list,
            "banshi kw": self._cmd_kw,
            "banshi maxdur": self._cmd_maxdur,
            "banshi interval": self._cmd_interval,
            "banshi mode": self._cmd_mode,
            "banshi addtime": self._cmd_addtime,
            "banshi deltime": self._cmd_deltime,
            "banshi reset": self._cmd_reset,
        }
        for cmd_prefix, handler in full_cmds.items():
            if msg == cmd_prefix or msg.startswith(cmd_prefix + " "):
                async for r in handler(event):
                    yield r
                return

        yield event.plain_result(f"未知指令: {parts[1]}\n输入 /banshi 查看帮助")

    # ========== 子命令实现 ==========

    def _get_event_group(self, event: AstrMessageEvent) -> Optional[str]:
        """获取事件来源的群号"""
        try:
            return str(event.message_obj.group_id)
        except:
            return None

    def _get_group_umo(self, group_id: str) -> Optional[str]:
        return self._config.get("bound_groups", {}).get(group_id)

    async def _cmd_on(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        gc = self._get_group_cfg(gid)
        if gc.get("enabled"):
            yield event.plain_result("本群搬石已经在运行了")
            return

        gc["enabled"] = True
        await self._save_config()

        # 确保定时任务在跑
        if not self.running:
            self.running = True
            self._shutdown = False
            self.task = asyncio.create_task(self._timer_task())

        yield event.plain_result(f"✅ 本群搬石已开启")

    async def _cmd_off(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        gc = self._get_group_cfg(gid)
        if not gc.get("enabled"):
            yield event.plain_result("本群搬石已经关闭了")
            return

        gc["enabled"] = False
        await self._save_config()

        # 检查是否所有群都关了，如果是则停止定时任务
        any_enabled = any(
            g.get("enabled") for g in self._config.get("group_configs", {}).values()
        )
        if not any_enabled and self.task:
            self.running = False
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=3.0)
            except:
                pass
            self.task = None

        yield event.plain_result(f"⏸ 本群搬石已关闭")

    async def _cmd_now(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        umo = self._get_group_umo(gid)
        if not umo:
            yield event.plain_result("❌ 本群尚未绑定")
            return

        yield event.plain_result("🔍 开始搬石...")

        # 单次执行
        video = await self._select_video(gid)
        if not video:
            yield event.plain_result("❌ 没有找到合适的视频（可能都发过了）")
            return

        bvid = video["bvid"]
        self._add_sent(gid, bvid)
        await self._save_config()

        detail = await self._get_video_detail(bvid)
        card = self._build_card(video, detail)
        chain = MessageChain([Plain(card)])
        await self.context.send_message(umo, chain)

    async def _cmd_list(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        gc = self._get_group_cfg(gid)
        keywords = self._get_group_keywords(gid)
        sent_count = len(self._get_sent_set(gid))

        mode = gc.get("scan_mode", "interval")
        interval = gc.get("scan_interval", 300)
        push_times = gc.get("push_times", [])
        mode_label = "⏱ 间隔推送" if mode == "interval" else "🕐 定时推送"
        schedule_info = f"每 {interval} 秒" if mode == "interval" else f"每天 {', '.join(push_times)}"

        lines = [
            f"=== 本群搬石状态 ===",
            f"运行状态: {'✅ 运行中' if gc.get('enabled') else '❌ 已关闭'}",
            f"推送模式: {mode_label}",
            f"推送频率: {schedule_info}",
            f"视频最大时长: {gc.get('max_duration', 0)}秒（0=不限）",
            f"搜索页数: {gc.get('max_pages', 3)}",
            f"关键词数: {len(keywords)} 个",
            f"已发送: {sent_count} 个视频",
            f"版本: 0.6.0",
        ]
        yield event.plain_result("\n".join(lines))

    async def _cmd_kw(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        msg = event.message_str.strip()
        rest = msg
        for prefix in ["banshi kw", "banshi"]:
            if rest.startswith(prefix):
                rest = rest[len(prefix):].strip()
                break

        gc = self._get_group_cfg(gid)

        if not rest:
            keywords = self._get_group_keywords(gid)
            yield event.plain_result(
                f"本群关键词 ({len(keywords)}个)：\n" +
                "\n".join(f"  · {k}" for k in keywords) +
                "\n\n用法：/banshi kw add <词>  或  /banshi kw del <词>"
            )
            return

        parts = rest.split()
        action = parts[0].lower()
        keyword = " ".join(parts[1:]) if len(parts) > 1 else ""

        if not keyword:
            yield event.plain_result("请输入关键词\n用法：/banshi kw add <词>  或  /banshi kw del <词>")
            return

        if "keywords" not in gc:
            gc["keywords"] = []

        if action == "add":
            if keyword not in gc["keywords"]:
                gc["keywords"].append(keyword)
                await self._save_config()
                yield event.plain_result(f"✅ 已添加关键词：{keyword}")
            else:
                yield event.plain_result("关键词已存在")

        elif action in ("del", "remove"):
            if keyword in gc["keywords"]:
                gc["keywords"].remove(keyword)
                await self._save_config()
                yield event.plain_result(f"🗑 已删除关键词：{keyword}")
            else:
                yield event.plain_result("未找到该关键词")
        else:
            yield event.plain_result(f"未知操作: {action}\n用法：/banshi kw add/del <关键词>")

    async def _cmd_maxdur(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        msg = event.message_str.strip()
        rest = msg
        for prefix in ["banshi maxdur", "banshi"]:
            if rest.startswith(prefix):
                rest = rest[len(prefix):].strip()
                break

        gc = self._get_group_cfg(gid)

        if not rest:
            yield event.plain_result(f"本群当前最大时长：{gc.get('max_duration', 600)}秒\n用法：/banshi maxdur <秒数>")
            return
        try:
            val = int(rest.split()[0])
            if val < 0:
                yield event.plain_result("时长不能为负数（0=不限时长）")
                return
            gc["max_duration"] = val
            await self._save_config()
            label = "不限时长" if val == 0 else f"{val}秒"
            yield event.plain_result(f"✅ 本群视频最大时长设为：{label}")
        except ValueError:
            yield event.plain_result("请输入有效数字")

    async def _cmd_interval(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        msg = event.message_str.strip()
        rest = msg
        for prefix in ["banshi interval", "banshi"]:
            if rest.startswith(prefix):
                rest = rest[len(prefix):].strip()
                break

        gc = self._get_group_cfg(gid)
        current = gc.get("scan_interval", 300)

        if not rest:
            yield event.plain_result(f"本群间隔推送间隔：{current}秒\n用法：/banshi interval <秒数>")
            return
        try:
            val = int(rest.split()[0])
            if val < 30:
                yield event.plain_result("间隔不能小于30秒")
                return
            gc["scan_interval"] = val
            # 自动切为间隔模式
            gc["scan_mode"] = "interval"
            await self._save_config()
            yield event.plain_result(f"✅ 本群间隔推送间隔设为：{val}秒（已自动切为间隔模式）")
        except ValueError:
            yield event.plain_result("请输入有效数字")

    async def _cmd_mode(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        msg = event.message_str.strip()
        rest = msg
        for prefix in ["banshi mode", "banshi"]:
            if rest.startswith(prefix):
                rest = rest[len(prefix):].strip()
                break

        gc = self._get_group_cfg(gid)
        if not rest:
            mode = gc.get("scan_mode", "interval")
            yield event.plain_result(f"当前模式：{'间隔推送' if mode == 'interval' else '定时推送'}\n用法：/banshi mode <interval|scheduled>")
            return

        mode = rest.split()[0].lower()
        if mode == "interval":
            gc["scan_mode"] = "interval"
            await self._save_config()
            yield event.plain_result(f"✅ 已切换为间隔推送模式（每 {gc.get('scan_interval', 300)} 秒）")
        elif mode == "scheduled":
            gc["scan_mode"] = "scheduled"
            await self._save_config()
            times = gc.get("push_times", [])
            yield event.plain_result(f"✅ 已切换为定时推送模式\n当前定时点：{', '.join(times) if times else '（无，请用 /banshi addtime 添加）'}")
        else:
            yield event.plain_result("模式只能是 interval（间隔推送）或 scheduled（定时推送）")

    async def _cmd_addtime(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        msg = event.message_str.strip()
        rest = msg
        for prefix in ["banshi addtime", "banshi"]:
            if rest.startswith(prefix):
                rest = rest[len(prefix):].strip()
                break

        if not rest:
            yield event.plain_result("用法：/banshi addtime <HH:MM>\n例如：/banshi addtime 20:00")
            return

        t = rest.split()[0]
        import re
        if not re.match(r"^[0-2][0-9]:[0-5][0-9]$", t):
            yield event.plain_result("时间格式错误，请使用 HH:MM（24小时制），例如 20:00")
            return

        gc = self._get_group_cfg(gid)
        if "push_times" not in gc:
            gc["push_times"] = []
        if t not in gc["push_times"]:
            gc["push_times"].append(t)
            gc["push_times"].sort()
            gc["scan_mode"] = "scheduled"
            await self._save_config()
            yield event.plain_result(f"✅ 已添加定时点 {t}\n当前定时点：{', '.join(gc['push_times'])}")
        else:
            yield event.plain_result(f"定时点 {t} 已存在")

    async def _cmd_deltime(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        msg = event.message_str.strip()
        rest = msg
        for prefix in ["banshi deltime", "banshi"]:
            if rest.startswith(prefix):
                rest = rest[len(prefix):].strip()
                break

        gc = self._get_group_cfg(gid)
        if not rest:
            times = gc.get("push_times", [])
            yield event.plain_result(f"当前定时点：{', '.join(times) if times else '（无）'}\n用法：/banshi deltime <HH:MM>")
            return

        t = rest.split()[0]
        if t in gc.get("push_times", []):
            gc["push_times"].remove(t)
            await self._save_config()
            times = gc.get("push_times", [])
            yield event.plain_result(f"✅ 已删除定时点 {t}\n当前定时点：{', '.join(times) if times else '（无）'}")
        else:
            yield event.plain_result(f"定时点 {t} 不存在")

    async def _cmd_reset(self, event: AstrMessageEvent):
        gid = self._get_event_group(event)
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令")
            return

        gc = self._get_group_cfg(gid)
        count = len(gc.get("sent_bvids", []))
        gc["sent_bvids"] = []
        await self._save_config()
        yield event.plain_result(f"✅ 已清空本群历史记录（{count}条），将重新发送已发过的视频")

    # ========== WebUI API（设置页后端） ==========

    async def api_get_groups(self):
        """返回所有群的配置"""
        from quart import jsonify
        groups = self._config.get("bound_groups", {})
        configs = self._config.get("group_configs", {})
        result = []
        for gid, umo in groups.items():
            gc = configs.get(gid, self._default_group_config())
            result.append({
                "group_id": gid,
                "enabled": gc.get("enabled", False),
                "keywords": gc.get("keywords", []),
                "max_duration": gc.get("max_duration", 600),
                "max_pages": gc.get("max_pages", 3),
                "sent_count": len(gc.get("sent_bvids", [])),
                "scan_mode": gc.get("scan_mode", "interval"),
                "scan_interval": gc.get("scan_interval", 300),
                "push_times": gc.get("push_times", []),
            })
        return jsonify({"ok": True, "data": result})

    async def api_update_group_config(self, group_id: str):
        """更新指定群的配置"""
        from quart import request, jsonify
        try:
            body = await request.get_json()
        except Exception:
            return jsonify({"ok": False, "message": "无效的请求体"})

        gc = self._get_group_cfg(group_id)

        if "enabled" in body:
            gc["enabled"] = bool(body["enabled"])
        if "keywords" in body and isinstance(body["keywords"], list):
            gc["keywords"] = body["keywords"]
        if "max_duration" in body:
            gc["max_duration"] = int(body["max_duration"])
        if "max_pages" in body:
            gc["max_pages"] = int(body["max_pages"])
        if "scan_mode" in body:
            gc["scan_mode"] = body["scan_mode"]
        if "scan_interval" in body:
            gc["scan_interval"] = int(body["scan_interval"])
        if "push_times" in body and isinstance(body["push_times"], list):
            gc["push_times"] = body["push_times"]

        await self._save_config()

        # 如果改变 enabled 状态，管理定时任务
        self._refresh_timer()

        return jsonify({"ok": True, "message": f"群 {group_id} 配置已更新"})

    async def api_reset_group_config(self, group_id: str):
        """重置指定群的配置为默认值"""
        from quart import jsonify

        gc = self._get_group_cfg(group_id)
        defaults = self._default_group_config()
        gc["keywords"] = defaults["keywords"].copy()
        gc["max_duration"] = defaults["max_duration"]
        gc["max_pages"] = defaults["max_pages"]
        gc["sent_bvids"] = []
        gc["enabled"] = False

        await self._save_config()
        self._refresh_timer()

        return jsonify({"ok": True, "message": f"群 {group_id} 已重置为默认配置"})

    def _refresh_timer(self):
        """检查是否需要启动或停止定时任务"""
        any_enabled = any(
            g.get("enabled") for g in self._config.get("group_configs", {}).values()
        )
        if any_enabled and not self.running:
            self.running = True
            self._shutdown = False
            self.task = asyncio.create_task(self._timer_task())
            logger.info("搬石定时任务已启动（通过 WebUI）")
        elif not any_enabled and self.running and self.task:
            self.running = False
            self.task.cancel()
            try:
                import asyncio
                fut = asyncio.ensure_future(asyncio.wait_for(self.task, timeout=3.0))
            except:
                pass
            self.task = None
            logger.info("搬石定时任务已停止（所有群已关闭）")
