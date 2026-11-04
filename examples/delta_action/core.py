"""Core tactical mechanics for Delta Action extraction shooter.

Implements:
1. Operator state machine (Role, States, Health, Stamina, Abilities, Bleedout).
2. Ballistics & Armor penetration system (Tiers 1-6, Durability degradation, Blunt trauma, Critical hitboxes).
3. Weapon firing, recoil dispersion, and ADS mechanics.
4. Loot inventory with secure container (Safe Box) preservation.
5. Extraction zones with countdown timers, zone contests, and extraction state transitions.
"""
from __future__ import annotations

import enum
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable


class OperatorState(enum.Enum):
    ACTIVE = "ACTIVE"
    DOWNED = "DOWNED"
    REVIVING = "REVIVING"
    EXTRACTED = "EXTRACTED"
    KIA = "KIA"


class OperatorRole(enum.Enum):
    ASSAULT = "ASSAULT"
    MEDIC = "MEDIC"
    RECON = "RECON"
    ENGINEER = "ENGINEER"


class BodyPart(enum.Enum):
    HEAD = "HEAD"
    CHEST = "CHEST"
    STOMACH = "STOMACH"
    LIMBS = "LIMBS"


class ExtractionZoneStatus(enum.Enum):
    INACTIVE = "INACTIVE"
    ACTIVE = "ACTIVE"
    CONTESTED = "CONTESTED"
    EXHAUSTED = "EXHAUSTED"


@dataclass
class Bullet:
    name: str
    caliber: str
    flesh_damage: float
    penetration_tier: int  # 1 to 6
    armor_damage_factor: float = 0.55  # Ratio of penetration damage transferred to armor durability
    velocity_mps: float = 850.0


@dataclass
class DamageResult:
    penetrated: bool
    final_damage: float
    armor_durability_loss: float
    blunt_trauma: float
    hit_part: BodyPart
    operator_killed_or_downed: bool = False


@dataclass
class ArmorPlate:
    name: str
    tier: int  # 1 to 6
    max_durability: float
    current_durability: float
    material: str = "ceramic"  # "steel", "ceramic", "composite"
    blunt_throughput: float = 0.18  # Percent of flesh damage transmitted as blunt trauma when non-penetrated

    @property
    def durability_ratio(self) -> float:
        if self.max_durability <= 0:
            return 0.0
        return max(0.0, min(1.0, self.current_durability / self.max_durability))

    @property
    def is_broken(self) -> bool:
        return self.current_durability <= 0.001

    def repair(self, amount: float) -> float:
        """Repair armor up to max durability. Returns amount actually repaired."""
        before = self.current_durability
        self.current_durability = min(self.max_durability, self.current_durability + amount)
        return self.current_durability - before

    def calculate_penetration_chance(self, bullet: Bullet) -> float:
        """Calculate penetration chance based on bullet tier vs armor tier & durability ratio."""
        if self.is_broken:
            return 1.0

        effective_tier = self.tier * (0.35 + 0.65 * self.durability_ratio)
        tier_diff = bullet.penetration_tier - effective_tier

        if tier_diff >= 1.5:
            return 0.98
        elif tier_diff >= 0.5:
            return 0.85
        elif tier_diff >= 0.0:
            return 0.50 + 0.30 * (1.0 - self.durability_ratio)
        elif tier_diff >= -1.0:
            return max(0.08, 0.25 * (1.0 - self.durability_ratio))
        else:
            return 0.02

    def absorb_shot(self, bullet: Bullet, rng: random.Random | None = None) -> tuple[bool, float, float]:
        """Compute hit against armor plate.

        Returns: (penetrated, final_flesh_damage, durability_lost)
        """
        r = rng.random() if rng else random.random()
        chance = self.calculate_penetration_chance(bullet)
        penetrated = r < chance

        durability_damage = bullet.flesh_damage * bullet.armor_damage_factor * (0.8 + 0.4 * (bullet.penetration_tier / max(1, self.tier)))
        actual_dur_loss = min(self.current_durability, durability_damage)
        self.current_durability = max(0.0, self.current_durability - actual_dur_loss)

        if penetrated:
            # Bullet breached armor: deals high flesh damage with minor velocity dampening
            dampening = 0.85 if self.durability_ratio > 0.5 else 0.95
            flesh_dmg = bullet.flesh_damage * dampening
            return True, flesh_dmg, actual_dur_loss
        else:
            # Stopped by armor: plate takes heavy impact, wearer absorbs blunt trauma
            blunt_dmg = bullet.flesh_damage * self.blunt_throughput
            return False, blunt_dmg, actual_dur_loss


@dataclass
class ShotResult:
    fired: bool
    hit: bool
    distance_m: float
    dispersion_rad: float
    damage_result: DamageResult | None = None
    ammo_remaining: int = 0
    message: str = ""


class Weapon:
    def __init__(
        self,
        name: str,
        caliber: str,
        chambered_bullet: Bullet,
        mag_capacity: int = 30,
        base_spread_rad: float = 0.015,
        ads_spread_mult: float = 0.25,
        effective_range_m: float = 120.0,
    ):
        self.name = name
        self.caliber = caliber
        self.bullet = chambered_bullet
        self.mag_capacity = mag_capacity
        self.current_ammo = mag_capacity
        self.base_spread_rad = base_spread_rad
        self.ads_spread_mult = ads_spread_mult
        self.effective_range_m = effective_range_m

    def reload(self) -> int:
        reloaded = self.mag_capacity - self.current_ammo
        self.current_ammo = self.mag_capacity
        return reloaded

    def fire_at(
        self,
        target: Operator,
        target_distance_m: float,
        is_ads: bool = False,
        aim_part: BodyPart = BodyPart.CHEST,
        rng: random.Random | None = None,
    ) -> ShotResult:
        if self.current_ammo <= 0:
            return ShotResult(
                fired=False,
                hit=False,
                distance_m=target_distance_m,
                dispersion_rad=0.0,
                ammo_remaining=0,
                message="Click! Magazine is empty.",
            )

        self.current_ammo -= 1
        rand = rng or random.Random()

        # Calculate dispersion
        dispersion = self.base_spread_rad * (self.ads_spread_mult if is_ads else 1.0)
        # Hit probability drops with distance and dispersion
        hit_radius = 0.45  # Standard tactical silhouette radius in meters
        cone_radius = target_distance_m * math.tan(dispersion)
        hit_prob = min(0.99, max(0.05, hit_radius / (hit_radius + cone_radius)))

        hit = rand.random() < hit_prob
        if not hit:
            return ShotResult(
                fired=True,
                hit=False,
                distance_m=target_distance_m,
                dispersion_rad=dispersion,
                ammo_remaining=self.current_ammo,
                message=f"Shot missed target at {target_distance_m:.1f}m (dispersion {dispersion:.4f} rad).",
            )

        # Apply damage to target
        dmg_res = target.take_hit(self.bullet, hit_part=aim_part, rng=rand)
        return ShotResult(
            fired=True,
            hit=True,
            distance_m=target_distance_m,
            dispersion_rad=dispersion,
            damage_result=dmg_res,
            ammo_remaining=self.current_ammo,
            message=f"Direct hit on {aim_part.value}! Dealt {dmg_res.final_damage:.1f} dmg.",
        )


@dataclass
class LootItem:
    id: str
    name: str
    value: int
    weight_kg: float = 0.5
    is_secure: bool = False


@dataclass
class Inventory:
    safe_box_capacity: int = 4
    backpack_capacity: int = 16
    safe_box: list[LootItem] = field(default_factory=list)
    backpack: list[LootItem] = field(default_factory=list)

    def add_loot(self, item: LootItem, to_safe_box: bool = False) -> bool:
        if to_safe_box or item.is_secure:
            if len(self.safe_box) < self.safe_box_capacity:
                item.is_secure = True
                self.safe_box.append(item)
                return True
        if len(self.backpack) < self.backpack_capacity:
            item.is_secure = False
            self.backpack.append(item)
            return True
        return False

    def secure_item_from_backpack(self, item_id: str) -> bool:
        """Transfer an item from backpack to safe box if room allows."""
        if len(self.safe_box) >= self.safe_box_capacity:
            return False
        for idx, it in enumerate(self.backpack):
            if it.id == item_id:
                target = self.backpack.pop(idx)
                target.is_secure = True
                self.safe_box.append(target)
                return True
        return False

    @property
    def total_secured_value(self) -> int:
        return sum(it.value for it in self.safe_box)

    @property
    def total_backpack_value(self) -> int:
        return sum(it.value for it in self.backpack)

    @property
    def total_extracted_value(self) -> int:
        return self.total_secured_value + self.total_backpack_value

    def drop_on_kia(self) -> list[LootItem]:
        """Backpack is lost on death, safe box is permanently preserved."""
        dropped = list(self.backpack)
        self.backpack.clear()
        return dropped


@dataclass
class HitBox:
    part: BodyPart
    damage_multiplier: float
    armor: ArmorPlate | None = None


class Operator:
    def __init__(
        self,
        callsign: str,
        role: OperatorRole = OperatorRole.ASSAULT,
        max_health: float = 100.0,
        max_stamina: float = 100.0,
    ):
        self.callsign = callsign
        self.role = role
        self.state = OperatorState.ACTIVE
        self.max_health = max_health
        self.health = max_health
        self.max_stamina = max_stamina
        self.stamina = max_stamina

        # Downed & revive mechanics
        self.bleedout_timer: float = 30.0  # Seconds before death if not revived
        self.has_self_revive_kit: bool = False

        # Loadout & Protection
        self.helmet: ArmorPlate | None = None
        self.vest: ArmorPlate | None = None
        self.inventory = Inventory()
        self.primary_weapon: Weapon | None = None

        # Role perks
        if self.role == OperatorRole.ASSAULT:
            self.max_stamina = 120.0
            self.stamina = 120.0
        elif self.role == OperatorRole.MEDIC:
            self.has_self_revive_kit = True

    @property
    def is_alive(self) -> bool:
        return self.state in (OperatorState.ACTIVE, OperatorState.DOWNED, OperatorState.REVIVING)

    def equip_armor(self, plate: ArmorPlate, slot: str = "vest") -> None:
        if slot.lower() == "helmet":
            self.helmet = plate
        else:
            self.vest = plate

    def take_hit(
        self,
        bullet: Bullet,
        hit_part: BodyPart = BodyPart.CHEST,
        rng: random.Random | None = None,
    ) -> DamageResult:
        if self.state in (OperatorState.KIA, OperatorState.EXTRACTED):
            return DamageResult(
                penetrated=False,
                final_damage=0.0,
                armor_durability_loss=0.0,
                blunt_trauma=0.0,
                hit_part=hit_part,
            )

        multiplier = 1.0
        armor: ArmorPlate | None = None

        if hit_part == BodyPart.HEAD:
            multiplier = 2.5
            armor = self.helmet
        elif hit_part == BodyPart.CHEST:
            multiplier = 1.0
            armor = self.vest
        elif hit_part == BodyPart.STOMACH:
            multiplier = 0.9
            armor = self.vest
        elif hit_part == BodyPart.LIMBS:
            multiplier = 0.6
            armor = None

        penetrated = True
        flesh_dmg = bullet.flesh_damage
        dur_loss = 0.0
        blunt = 0.0

        if armor and not armor.is_broken:
            penetrated, absorbed_dmg, dur_loss = armor.absorb_shot(bullet, rng=rng)
            if penetrated:
                flesh_dmg = absorbed_dmg
            else:
                flesh_dmg = absorbed_dmg  # Blunt trauma transmitted
                blunt = absorbed_dmg

        final_dmg = flesh_dmg * multiplier
        self.health = max(0.0, self.health - final_dmg)

        downed_or_killed = False
        if self.health <= 0.0:
            downed_or_killed = True
            if self.state == OperatorState.ACTIVE:
                self.state = OperatorState.DOWNED
                self.health = 0.0
            elif self.state in (OperatorState.DOWNED, OperatorState.REVIVING):
                # Executed while downed
                self.die()

        return DamageResult(
            penetrated=penetrated,
            final_damage=final_dmg,
            armor_durability_loss=dur_loss,
            blunt_trauma=blunt,
            hit_part=hit_part,
            operator_killed_or_downed=downed_or_killed,
        )

    def die(self) -> None:
        self.state = OperatorState.KIA
        self.health = 0.0
        self.inventory.drop_on_kia()

    def tick(self, dt: float) -> None:
        """Update operator status over time (bleedout, stamina recovery)."""
        if self.state == OperatorState.DOWNED:
            self.bleedout_timer -= dt
            if self.bleedout_timer <= 0.0:
                self.die()
        elif self.state == OperatorState.ACTIVE:
            # Natural stamina regeneration
            if self.stamina < self.max_stamina:
                self.stamina = min(self.max_stamina, self.stamina + 8.0 * dt)

    def revive(self, revive_kit_used: bool = False) -> bool:
        if self.state not in (OperatorState.DOWNED, OperatorState.REVIVING):
            return False
        if not revive_kit_used and self.state == OperatorState.DOWNED and self.has_self_revive_kit:
            self.has_self_revive_kit = False
            revive_kit_used = True

        if revive_kit_used or self.state == OperatorState.REVIVING:
            self.state = OperatorState.ACTIVE
            self.health = self.max_health * 0.35  # Revived with 35% health
            self.bleedout_timer = 30.0
            return True
        return False

    def use_tactical_ability(self) -> str:
        """Execute role-specific tactical active ability."""
        if self.state != OperatorState.ACTIVE:
            return "Cannot use ability: operator is not active."

        if self.role == OperatorRole.MEDIC:
            healed = min(self.max_health - self.health, 40.0)
            self.health += healed
            return f"Stim shot applied! Healed {healed:.1f} HP."
        elif self.role == OperatorRole.ENGINEER:
            if self.vest:
                repaired = self.vest.repair(35.0)
                return f"Armor plates swapped! Restored {repaired:.1f} durability."
            return "No vest equipped to repair."
        elif self.role == OperatorRole.ASSAULT:
            self.stamina = self.max_stamina
            return "Tactical adrenaline surge! Stamina fully replenished."
        elif self.role == OperatorRole.RECON:
            return "Recon arrow deployed! Enemy extraction zones revealed."
        return "Tactical ability activated."


class ExtractionZone:
    def __init__(
        self,
        zone_id: str,
        name: str,
        required_countdown_s: float = 5.0,
    ):
        self.zone_id = zone_id
        self.name = name
        self.required_countdown = required_countdown_s
        self.timer: float = 0.0
        self.status = ExtractionZoneStatus.ACTIVE
        self.occupants: set[Operator] = set()

    def enter_zone(self, operator: Operator) -> bool:
        if self.status != ExtractionZoneStatus.ACTIVE or not operator.is_alive:
            return False
        self.occupants.add(operator)
        return True

    def leave_zone(self, operator: Operator) -> None:
        if operator in self.occupants:
            self.occupants.remove(operator)
            if not self.occupants:
                self.timer = 0.0

    def tick(self, dt: float) -> list[Operator]:
        """Progress countdown. When time completes, extracts all active occupants."""
        extracted: list[Operator] = []
        if self.status != ExtractionZoneStatus.ACTIVE or not self.occupants:
            self.timer = 0.0
            return extracted

        # Filter out dead occupants
        alive_occupants = [op for op in self.occupants if op.is_alive and op.state == OperatorState.ACTIVE]
        if not alive_occupants:
            self.timer = 0.0
            return extracted

        self.timer += dt
        if self.timer >= self.required_countdown:
            for op in alive_occupants:
                op.state = OperatorState.EXTRACTED
                extracted.append(op)
            self.occupants.clear()
            self.timer = 0.0
            self.status = ExtractionZoneStatus.EXHAUSTED
        return extracted


class TacticalMatch:
    def __init__(self, match_id: str, max_duration_s: float = 600.0):
        self.match_id = match_id
        self.max_duration = max_duration_s
        self.elapsed: float = 0.0
        self.operators: list[Operator] = []
        self.extraction_zones: list[ExtractionZone] = []

    def add_operator(self, op: Operator) -> None:
        self.operators.append(op)

    def add_extraction_zone(self, zone: ExtractionZone) -> None:
        self.extraction_zones.append(zone)

    def step(self, dt: float) -> dict[str, Any]:
        self.elapsed += dt
        for op in self.operators:
            op.tick(dt)

        all_extracted: list[Operator] = []
        for zone in self.extraction_zones:
            extracted = zone.tick(dt)
            all_extracted.extend(extracted)

        # Match timeout KIA check
        if self.elapsed >= self.max_duration:
            for op in self.operators:
                if op.state == OperatorState.ACTIVE or op.state == OperatorState.DOWNED:
                    op.die()

        return {
            "elapsed": self.elapsed,
            "extracted_count": len(all_extracted),
            "alive_count": sum(1 for op in self.operators if op.is_alive),
            "match_over": self.elapsed >= self.max_duration or all(not op.is_alive or op.state == OperatorState.EXTRACTED for op in self.operators),
        }
