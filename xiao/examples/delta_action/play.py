#!/usr/bin/env python3
"""Tactical Action Game — Delta Force: Extraction Raid (三角洲行动: 撤离突围).

Interactive terminal CLI action game implementing all Delta Action mechanics:
- Operator selection (Assault, Medic, Recon, Engineer)
- Realistic ballistics, armor tier mitigation, body hitboxes & critical headshots
- Live tactical combat encounters with enemy operative patrols
- Safe Box preservation: High-value loot locked in safe box is kept even upon KIA!
- Extraction zone countdown with risk-reward combat choices.

Can be played interactively or run in automated demonstration mode (`--demo`).
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from examples.delta_action.core import (
        ArmorPlate,
        BodyPart,
        Bullet,
        DamageResult,
        ExtractionZone,
        ExtractionZoneStatus,
        Inventory,
        LootItem,
        Operator,
        OperatorRole,
        OperatorState,
        ShotResult,
        TacticalMatch,
        Weapon,
    )
except ImportError:
    from core import (  # type: ignore
        ArmorPlate,
        BodyPart,
        Bullet,
        DamageResult,
        ExtractionZone,
        ExtractionZoneStatus,
        Inventory,
        LootItem,
        Operator,
        OperatorRole,
        OperatorState,
        ShotResult,
        TacticalMatch,
        Weapon,
    )

# Terminal ANSI colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_CYAN = "\033[36m"
C_GRAY = "\033[90m"


def print_banner() -> None:
    banner = f"""
{C_CYAN}{C_BOLD}╔═══════════════════════════════════════════════════════════════════════════╗
║          DELTA ACTION : EXTRACTION RAID (三角洲行动: 绝地撤离)            ║
║     战术撤离射击终端核心引擎 · 护甲弹道穿透 · 身体判定 · 安全箱物资保底   ║
╚═══════════════════════════════════════════════════════════════════════════╝{C_RESET}
"""
    print(banner)


def create_operator_presets() -> dict[str, tuple[str, OperatorRole, str]]:
    return {
        "1": ("【红狼】Raptor", OperatorRole.ASSAULT, "突击兵: 120 超高初始耐力, 战术技能瞬间回满耐力"),
        "2": ("【蜂医】Doc", OperatorRole.MEDIC, "医疗兵: 自带激素自救针(倒地可自救), 战术技能治疗 40 HP"),
        "3": ("【骇爪】Hawk", OperatorRole.RECON, "侦查兵: 战术侦查箭矢探测全图敌情, 瞄准精准度极高"),
        "4": ("【牧羊人】Spark", OperatorRole.ENGINEER, "工程兵: 战场防弹板快换技术, 战术技能恢复 35 点防弹插板耐久"),
    }


def create_weapons() -> dict[str, Weapon]:
    m995_bullet = Bullet("5.56mm M995 AP", "5.56x45mm", flesh_damage=45.0, penetration_tier=5, armor_damage_factor=0.6)
    m855_bullet = Bullet("5.56mm M855 FMJ", "5.56x45mm", flesh_damage=42.0, penetration_tier=3, armor_damage_factor=0.5)
    snb_bullet = Bullet("7.62mm SNB 穿甲弹", "7.62x54mm", flesh_damage=78.0, penetration_tier=6, armor_damage_factor=0.7)

    return {
        "M4A1": Weapon("M4A1 突击步枪", "5.56x45mm", chambered_bullet=m995_bullet, mag_capacity=30, base_spread_rad=0.012, ads_spread_mult=0.25),
        "M16A4": Weapon("M16A4 战术步枪", "5.56x45mm", chambered_bullet=m855_bullet, mag_capacity=30, base_spread_rad=0.010, ads_spread_mult=0.20),
        "SV-98": Weapon("SV-98 狙击步枪", "7.62x54mm", chambered_bullet=snb_bullet, mag_capacity=10, base_spread_rad=0.003, ads_spread_mult=0.10),
    }


def create_loot_pool() -> list[LootItem]:
    return [
        LootItem("gpu-01", "绝密显卡 (RTX 4090)", value=550000, weight_kg=1.8),
        LootItem("intel-02", "军工级加密闪存盘", value=180000, weight_kg=0.1),
        LootItem("skull-03", "纯金战术骷髅雕像", value=320000, weight_kg=2.5),
        LootItem("salewa-04", "萨列瓦高级战术急救包", value=35000, weight_kg=0.8),
        LootItem("optics-05", "热成像瞄准镜", value=120000, weight_kg=0.9),
        LootItem("stim-06", "军用兴奋药剂", value=45000, weight_kg=0.2),
    ]


class GameSession:
    def __init__(self, is_demo: bool = False):
        self.is_demo = is_demo
        self.rng = random.Random(42 if is_demo else None)
        self.operator: Operator = None  # type: ignore
        self.extraction_zone = ExtractionZone("cliff-heli", "绝壁停机坪撤离点", required_countdown_s=5.0)
        self.match = TacticalMatch("DELTA-RAID-701", max_duration_s=180.0)
        self.match.add_extraction_zone(self.extraction_zone)
        self.turn = 1

    def print_hud(self) -> None:
        op = self.operator
        status_color = C_GREEN if op.state == OperatorState.ACTIVE else (C_YELLOW if op.state == OperatorState.DOWNED else C_RED)
        hp_bar_len = 20
        hp_fill = int((op.health / op.max_health) * hp_bar_len)
        hp_bar = f"{'█' * hp_fill}{'░' * (hp_bar_len - hp_fill)}"

        vest_info = f"等级 {op.vest.tier} [{op.vest.current_durability:.0f}/{op.vest.max_durability:.0f}]" if op.vest else "无"
        helm_info = f"等级 {op.helmet.tier} [{op.helmet.current_durability:.0f}/{op.helmet.max_durability:.0f}]" if op.helmet else "无"
        ammo_info = f"{op.primary_weapon.current_ammo}/{op.primary_weapon.mag_capacity}" if op.primary_weapon else "无"

        print(f"\n{C_GRAY}{'─' * 75}{C_RESET}")
        print(f"【干员】: {C_BOLD}{op.callsign}{C_RESET} ({op.role.value}) | 状态: {status_color}{op.state.value}{C_RESET} | 剩余战局时间: {self.match.max_duration - self.match.elapsed:.0f}s")
        print(f"【生命值】: [{C_GREEN}{hp_bar}{C_RESET}] {op.health:.1f}/{op.max_health:.0f} HP | 耐力: {op.stamina:.0f}/{op.max_stamina:.0f}")
        print(f"【防护】: 头盔: {helm_info} | 防弹衣: {vest_info} | 弹药: {ammo_info}")
        print(f"【安全箱保底】: {len(op.inventory.safe_box)}件 (价值: ¥{op.inventory.total_secured_value:,}) | 【背包物资】: {len(op.inventory.backpack)}件 (价值: ¥{op.inventory.total_backpack_value:,})")
        print(f"{C_GRAY}{'─' * 75}{C_RESET}")

    def setup_operator(self, choice: str = "2") -> None:
        presets = create_operator_presets()
        if choice not in presets:
            choice = "2"
        name, role, desc = presets[choice]
        self.operator = Operator(callsign=name, role=role)

        # Equip armor & weapon
        helmet = ArmorPlate("防暴快反头盔", tier=4, max_durability=45.0, current_durability=45.0)
        vest = ArmorPlate("6B45 重装插板防弹衣", tier=5, max_durability=75.0, current_durability=75.0)
        self.operator.equip_armor(helmet, slot="helmet")
        self.operator.equip_armor(vest, slot="vest")

        weapons = create_weapons()
        self.operator.primary_weapon = weapons["M4A1"]
        self.match.add_operator(self.operator)

        print(f"\n[部署成功] 干员: {C_BOLD}{name}{C_RESET} 已潜入战区！")
        print(f"特战特性: {desc}")
        print(f"标准配发: 4级防弹头盔 + 5级复合陶瓷插板防弹衣 + M4A1 突击步枪 (装填 5.56mm M995 穿甲弹)")

    def encounter_combat(self) -> None:
        print(f"\n{C_RED}{C_BOLD}⚠️ [遭遇战触发] 前方侦测到敌方巡逻小队（阿萨拉武装精锐）！{C_RESET}")
        enemy_bullets = [
            Bullet("7.62x39mm PS 步枪弹", "7.62x39mm", flesh_damage=48.0, penetration_tier=3),
            Bullet("5.45x39mm BT 穿甲弹", "5.45x39mm", flesh_damage=44.0, penetration_tier=4),
        ]
        chosen_bullet = self.rng.choice(enemy_bullets)

        # Player fires first
        print(f"\n你立即端起 {self.operator.primary_weapon.name} 组织反击！")
        if self.is_demo:
            action = "1"
        else:
            print("1. 【举枪开镜瞄准 (ADS)】精准射击敌方胸部（高命中、低后坐）")
            print("2. 【战术强突】瞄准头部尝试致命一击（爆头暴击 2.5 倍）")
            print("3. 【使用战术特勤技能】（医疗/装甲/耐力）")
            try:
                action = input("请选择你的战术行动 [1-3]: ").strip()
            except (EOFError, KeyboardInterrupt):
                action = "1"

        if action == "3":
            msg = self.operator.use_tactical_ability()
            print(f"{C_GREEN}[战术技能释放] {msg}{C_RESET}")
        else:
            is_ads = (action == "1")
            aim = BodyPart.CHEST if is_ads else BodyPart.HEAD
            print(f"射击方式: {'开镜精准射击 (ADS)' if is_ads else '战术甩枪爆头'}，射击部位: {aim.value}...")
            # Simulate firing at enemy
            dummy_enemy = Operator("Enemy Raider", max_health=80.0)
            dummy_enemy.equip_armor(ArmorPlate("敌方战术背心", tier=3, max_durability=40.0, current_durability=40.0))
            shot_res = self.operator.primary_weapon.fire_at(dummy_enemy, target_distance_m=25.0, is_ads=is_ads, aim_part=aim, rng=self.rng)
            if shot_res.hit:
                print(f"{C_GREEN}命中目标！造成 {shot_res.damage_result.final_damage:.1f} 点穿透伤害！成功压制敌方！{C_RESET}")
            else:
                print(f"{C_YELLOW}未命中目标！弹着点偏移。{C_RESET}")

        # Enemy return fire
        print(f"\n敌方精锐使用 {chosen_bullet.name} 进行火力还击！")
        hit_part = self.rng.choice([BodyPart.CHEST, BodyPart.CHEST, BodyPart.HEAD, BodyPart.LIMBS])
        dmg_res = self.operator.take_hit(chosen_bullet, hit_part=hit_part, rng=self.rng)

        if dmg_res.penetrated:
            print(f"{C_RED}⚠️ 警告: 敌方子弹击穿了你的 {hit_part.value} 防护！造成 {dmg_res.final_damage:.1f} 点剧烈肉体伤害！{C_RESET}")
        else:
            print(f"{C_GREEN}🛡️ 防弹插板成功挡住子弹！吸收大部分能量，仅受到 {dmg_res.final_damage:.1f} 点钝击创伤 (插板耐久损耗: {dmg_res.armor_durability_loss:.1f}){C_RESET}")

        if self.operator.state == OperatorState.DOWNED:
            print(f"\n{C_RED}{C_BOLD}💥 你已被击倒！进入濒死状态！流血倒计时 30 秒！{C_RESET}")
            if self.operator.has_self_revive_kit:
                print(f"{C_CYAN}医疗干员激素自救针触发！正在注射自救药剂...{C_RESET}")
                self.operator.revive()
                print(f"{C_GREEN}自救成功！重返战斗状态！恢复至 {self.operator.health:.0f} HP！{C_RESET}")

    def search_loot(self) -> None:
        pool = create_loot_pool()
        found = self.rng.sample(pool, k=2)
        print(f"\n{C_YELLOW}📦 搜索军用物资箱成功！发现高价值物资：{C_RESET}")
        for idx, item in enumerate(found, 1):
            print(f"  [{idx}] {C_BOLD}{item.name}{C_RESET} | 估值: ¥{item.value:,} | 重量: {item.weight_kg}kg")

        for item in found:
            # High-value items auto placed in safe box if room
            if item.value >= 150000 and len(self.operator.inventory.safe_box) < self.operator.inventory.safe_box_capacity:
                self.operator.inventory.add_loot(item, to_safe_box=True)
                print(f"  {C_GREEN}🔒 已将高价值密文资产 [{item.name}] 放入【安全箱】！(即使战死也绝对保底带出){C_RESET}")
            else:
                self.operator.inventory.add_loot(item, to_safe_box=False)
                print(f"  🎒 已将 [{item.name}] 放入【战术背包】(若战死将掉落)")

    def attempt_extraction(self) -> bool:
        print(f"\n{C_CYAN}{C_BOLD}🚁 抵达【{self.extraction_zone.name}】！信号绿烟已点燃！{C_RESET}")
        self.extraction_zone.enter_zone(self.operator)
        countdown = self.extraction_zone.required_countdown
        print(f"撤离倒计时开始: 需要在区域内坚守 {countdown:.0f} 秒...")

        for sec in range(int(countdown), 0, -1):
            print(f"  >> 撤离直升机正在索降... 倒计时: {sec}s")
            self.match.step(1.0)
            if not self.is_demo:
                time.sleep(0.5)

        if self.operator.state == OperatorState.EXTRACTED:
            print(f"\n{C_GREEN}{C_BOLD}🎉 登机成功！撤离直升机拉升脱离交火区！{C_RESET}")
            return True
        return False

    def print_post_raid_summary(self) -> None:
        op = self.operator
        print(f"\n{C_BOLD}═══════════════════════════════════════════════════════════════════════════{C_RESET}")
        if op.state == OperatorState.EXTRACTED:
            print(f"{C_GREEN}{C_BOLD}       战局胜利: 全面安全撤离 (SUCCESSFUL EXTRACTION)      {C_RESET}")
            total_val = op.inventory.total_extracted_value
            print(f"本次搜刮总战果: {C_BOLD}¥{total_val:,}{C_RESET}")
            print(f"  - 安全箱带出物资: ¥{op.inventory.total_secured_value:,} (共 {len(op.inventory.safe_box)} 件)")
            print(f"  - 背包带出物资:   ¥{op.inventory.total_backpack_value:,} (共 {len(op.inventory.backpack)} 件)")
        else:
            print(f"{C_RED}{C_BOLD}       战局失利: 阵亡 (KILLED IN ACTION)      {C_RESET}")
            print("背包中的所有常规物资已全部在战区遗失掉落！")
            print(f"{C_GREEN}{C_BOLD}🛡️ 但安全箱保底机制生效！以下核心资产已被保密带回基地：{C_RESET}")
            for it in op.inventory.safe_box:
                print(f"  ✓ [安全箱安全带回] {it.name} (价值: ¥{it.value:,})")
            print(f"保底资产总价值: {C_BOLD}¥{op.inventory.total_secured_value:,}{C_RESET}")
        print(f"{C_BOLD}═══════════════════════════════════════════════════════════════════════════{C_RESET}\n")

    def run(self) -> int:
        print_banner()
        if not self.is_demo:
            print("请选择你的特勤干员：")
            presets = create_operator_presets()
            for k, (name, role, desc) in presets.items():
                print(f"  [{k}] {name} - {desc}")
            try:
                choice = input("\n请选择干员编号 [默认 2-蜂医]: ").strip() or "2"
            except (EOFError, KeyboardInterrupt):
                choice = "2"
        else:
            print(f"{C_YELLOW}[自动化演示模式已开启 (--demo)] 正在自动模拟完整三角洲撤离战局...{C_RESET}")
            choice = "2"

        self.setup_operator(choice)

        # Raid Loop
        while self.operator.is_alive and self.operator.state != OperatorState.EXTRACTED and self.match.elapsed < self.match.max_duration:
            self.print_hud()
            if self.is_demo:
                if self.turn == 1:
                    print("\n[演示动作 1] 前往物资区搜寻高价值装备与加密资产...")
                    self.search_loot()
                elif self.turn == 2:
                    print("\n[演示动作 2] 侦测到敌军突袭，进入战术交火...")
                    self.encounter_combat()
                elif self.turn == 3:
                    print("\n[演示动作 3] 战果丰硕，向撤离点快速机动并呼叫撤离...")
                    success = self.attempt_extraction()
                    if success:
                        break
                self.turn += 1
                self.match.step(15.0)
            else:
                print("\n【战术决策】:")
                print("1. 搜刮附近军用物资箱 (可能获取显卡、绝密金骷髅)")
                print("2. 扫荡敌军巡逻队 (战术交火、获取高等级护甲弹药)")
                print("3. 使用干员专属战术技能 (治疗/补甲/耐力充能)")
                print("4. 前往绝壁停机坪组织撤离")
                print("5. 退出战局")
                try:
                    action = input("请输入行动编号 [1-5]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break

                if action == "1":
                    self.search_loot()
                    self.match.step(20.0)
                elif action == "2":
                    self.encounter_combat()
                    self.match.step(30.0)
                elif action == "3":
                    msg = self.operator.use_tactical_ability()
                    print(f"{C_GREEN}[技能施放] {msg}{C_RESET}")
                    self.match.step(5.0)
                elif action == "4":
                    success = self.attempt_extraction()
                    if success:
                        break
                elif action == "5":
                    print("中止行动，返回大厅。")
                    break

        self.print_post_raid_summary()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Delta Action extraction tactical raid game.")
    parser.add_argument("--demo", action="store_true", help="Run automated walkthrough demo without waiting for interactive input.")
    args = parser.parse_args()

    session = GameSession(is_demo=args.demo)
    return session.run()


if __name__ == "__main__":
    sys.exit(main())
