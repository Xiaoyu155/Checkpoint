from __future__ import annotations

import random
from examples.delta_action.core import (
    ArmorPlate,
    BodyPart,
    Bullet,
    ExtractionZone,
    ExtractionZoneStatus,
    Inventory,
    LootItem,
    Operator,
    OperatorRole,
    OperatorState,
    TacticalMatch,
    Weapon,
)


def test_operator_initialization_and_perks() -> None:
    assault = Operator(callsign="Raptor", role=OperatorRole.ASSAULT)
    assert assault.state == OperatorState.ACTIVE
    assert assault.health == 100.0
    assert assault.stamina == 120.0  # Assault perk
    assert assault.is_alive is True

    medic = Operator(callsign="Doc", role=OperatorRole.MEDIC)
    assert medic.has_self_revive_kit is True  # Medic perk


def test_armor_penetration_and_blunt_trauma() -> None:
    # High-tier bullet against low-tier armor -> should penetrate with high probability
    level_2_vest = ArmorPlate(name="PACA Vest", tier=2, max_durability=40.0, current_durability=40.0)
    ap_bullet = Bullet(name="M995", caliber="5.56x45mm", flesh_damage=42.0, penetration_tier=5)

    chance = level_2_vest.calculate_penetration_chance(ap_bullet)
    assert chance >= 0.85

    penetrated, dmg, dur_loss = level_2_vest.absorb_shot(ap_bullet, rng=random.Random(42))
    assert penetrated is True
    assert dur_loss > 0
    assert dmg > 30.0

    # Low-tier pistol bullet against high-tier ceramic armor -> stopped, dealing blunt trauma
    level_5_vest = ArmorPlate(name="Defender-2", tier=5, max_durability=75.0, current_durability=75.0)
    pst_bullet = Bullet(name="9x19 Pst gzh", caliber="9x19mm", flesh_damage=54.0, penetration_tier=2)

    chance_low = level_5_vest.calculate_penetration_chance(pst_bullet)
    assert chance_low <= 0.05

    penetrated2, blunt_dmg, dur_loss2 = level_5_vest.absorb_shot(pst_bullet, rng=random.Random(42))
    assert penetrated2 is False
    assert blunt_dmg < 15.0  # Only blunt trauma transferred
    assert dur_loss2 > 0


def test_hitbox_and_critical_headshots() -> None:
    op = Operator(callsign="Viper")
    helmet = ArmorPlate(name="Fast MT", tier=4, max_durability=40.0, current_durability=40.0)
    op.equip_armor(helmet, slot="helmet")

    sniper_bullet = Bullet(name="7.62x54mm SNB", caliber="7.62x54mm", flesh_damage=75.0, penetration_tier=6)

    # Headshot with Tier 6 sniper round breaches helmet -> 2.5x critical multiplier
    res = op.take_hit(sniper_bullet, hit_part=BodyPart.HEAD, rng=random.Random(42))
    assert res.penetrated is True
    assert res.final_damage >= 150.0  # Massive critical hit
    assert op.state == OperatorState.DOWNED


def test_operator_downed_bleedout_and_revive() -> None:
    op = Operator(callsign="Ghost", role=OperatorRole.ASSAULT)
    # Fatal chest damage to down operator
    heavy_shot = Bullet(name="12/70 Slug", caliber="12g", flesh_damage=120.0, penetration_tier=1)
    op.take_hit(heavy_shot, hit_part=BodyPart.CHEST)

    assert op.state == OperatorState.DOWNED
    assert op.health == 0.0
    assert op.bleedout_timer == 30.0

    # Bleedout progression
    op.tick(15.0)
    assert op.bleedout_timer == 15.0
    assert op.state == OperatorState.DOWNED

    # Successful revive via teammate
    revived = op.revive(revive_kit_used=True)
    assert revived is True
    assert op.state == OperatorState.ACTIVE
    assert op.health == 35.0  # 35% restored
    assert op.bleedout_timer == 30.0

    # Down again and let timer expire -> KIA
    op.take_hit(heavy_shot, hit_part=BodyPart.CHEST)
    assert op.state == OperatorState.DOWNED
    op.tick(35.0)
    assert op.state == OperatorState.KIA
    assert op.is_alive is False


def test_medic_self_revive() -> None:
    medic = Operator(callsign="Doc", role=OperatorRole.MEDIC)
    assert medic.has_self_revive_kit is True

    shot = Bullet(name="Grenade Shrapnel", caliber="HE", flesh_damage=150.0, penetration_tier=1)
    medic.take_hit(shot, hit_part=BodyPart.CHEST)
    assert medic.state == OperatorState.DOWNED

    # Medic uses self-revive kit
    revived = medic.revive()
    assert revived is True
    assert medic.state == OperatorState.ACTIVE
    assert medic.has_self_revive_kit is False  # Expended kit


def test_tactical_abilities() -> None:
    medic = Operator(callsign="Doc", role=OperatorRole.MEDIC)
    medic.health = 40.0
    msg = medic.use_tactical_ability()
    assert "Stim shot" in msg
    assert medic.health == 80.0

    eng = Operator(callsign="Spark", role=OperatorRole.ENGINEER)
    vest = ArmorPlate(name="6B23", tier=3, max_durability=60.0, current_durability=20.0)
    eng.equip_armor(vest, slot="vest")
    msg2 = eng.use_tactical_ability()
    assert "Armor plates swapped" in msg2
    assert vest.current_durability == 55.0


def test_weapon_firing_mechanics() -> None:
    target = Operator(callsign="Dummy")
    bullet = Bullet(name="M855", caliber="5.56x45mm", flesh_damage=40.0, penetration_tier=3)
    rifle = Weapon(
        name="M4A1",
        caliber="5.56x45mm",
        chambered_bullet=bullet,
        mag_capacity=3,
        base_spread_rad=0.01,
        ads_spread_mult=0.2,
    )

    # Shot 1: ADS fire at close range (5m) -> accurate hit
    res1 = rifle.fire_at(target, target_distance_m=5.0, is_ads=True, aim_part=BodyPart.CHEST, rng=random.Random(1))
    assert res1.fired is True
    assert res1.ammo_remaining == 2
    assert res1.hit is True
    assert target.health < 100.0

    # Fire rest of mag
    rifle.fire_at(target, 5.0)
    rifle.fire_at(target, 5.0)
    assert rifle.current_ammo == 0

    # Firing when empty fails
    res_empty = rifle.fire_at(target, 5.0)
    assert res_empty.fired is False
    assert "Magazine is empty" in res_empty.message

    # Reload
    reloaded = rifle.reload()
    assert reloaded == 3
    assert rifle.current_ammo == 3


def test_inventory_safe_box_preservation_on_kia() -> None:
    inv = Inventory()
    gpu = LootItem(id="gpu-1", name="Graphics Card", value=500000, is_secure=True)
    intel = LootItem(id="intel-1", name="Encrypted Flash Drive", value=150000, is_secure=False)
    salewa = LootItem(id="med-1", name="Salewa First Aid", value=30000, is_secure=False)

    assert inv.add_loot(gpu, to_safe_box=True) is True
    assert inv.add_loot(intel, to_safe_box=False) is True
    assert inv.add_loot(salewa, to_safe_box=False) is True

    # Move intel to safe box
    assert inv.secure_item_from_backpack("intel-1") is True
    assert len(inv.safe_box) == 2
    assert len(inv.backpack) == 1

    assert inv.total_secured_value == 650000
    assert inv.total_backpack_value == 30000

    # Operator is KIA: backpack items drop to ground, safe box items remain 100% secured
    dropped = inv.drop_on_kia()
    assert len(dropped) == 1
    assert dropped[0].id == "med-1"
    assert len(inv.backpack) == 0
    assert len(inv.safe_box) == 2
    assert inv.total_secured_value == 650000


def test_extraction_zone_countdown_and_escape() -> None:
    zone = ExtractionZone(zone_id="zone-cliff", name="Cliffside Helicopter", required_countdown_s=4.0)
    op = Operator(callsign="Nomad")

    assert zone.enter_zone(op) is True
    assert op in zone.occupants

    # Tick 2 seconds (halfway)
    extracted = zone.tick(2.0)
    assert len(extracted) == 0
    assert zone.timer == 2.0
    assert op.state == OperatorState.ACTIVE

    # Operator leaves zone early -> timer cancels
    zone.leave_zone(op)
    assert zone.timer == 0.0

    # Re-enter and wait full 4 seconds
    zone.enter_zone(op)
    zone.tick(2.5)
    extracted2 = zone.tick(2.0)

    assert len(extracted2) == 1
    assert extracted2[0].callsign == "Nomad"
    assert op.state == OperatorState.EXTRACTED
    assert zone.status == ExtractionZoneStatus.EXHAUSTED


def test_tactical_match_timeout_kia() -> None:
    match = TacticalMatch(match_id="delta-match-001", max_duration_s=100.0)
    op1 = Operator(callsign="Alpha")
    op2 = Operator(callsign="Bravo")
    match.add_operator(op1)
    match.add_operator(op2)

    # Step forward 50 seconds
    status1 = match.step(50.0)
    assert status1["elapsed"] == 50.0
    assert status1["alive_count"] == 2
    assert status1["match_over"] is False

    # Exceed match duration -> operators fail to extract in time and become KIA
    status2 = match.step(60.0)
    assert status2["elapsed"] == 110.0
    assert status2["match_over"] is True
    assert op1.state == OperatorState.KIA
    assert op2.state == OperatorState.KIA
    assert status2["alive_count"] == 0


def test_interactive_game_session_demo() -> None:
    from examples.delta_action.play import GameSession
    session = GameSession(is_demo=True)
    exit_code = session.run()
    assert exit_code == 0
    assert session.operator.state == OperatorState.EXTRACTED
    assert session.operator.inventory.total_secured_value >= 500000

