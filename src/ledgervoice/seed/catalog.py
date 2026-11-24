"""Fictional merchants and personas for the seed (D24). Data only; no logic.

All names and amounts are invented. Categories use the fixed taxonomy from D24 plus
"travel" (D50). Each persona's expected monthly budget must not be negative; a test
checks this so a tuning change can't silently create an overspending persona. Amount ranges are
whole dollars (D16). Tune spending here; the generator reads it and the golden test
will fail on purpose when anything here changes (update the digest deliberately).
"""

from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = (
    "groceries",
    "dining",
    "coffee",
    "transport",
    "housing",
    "utilities",
    "subscriptions",
    "shopping",
    "entertainment",
    "health",
    "travel",  # D50: flights and hotels, so "housing" means rent
)

# key -> (name, category)
MERCHANTS: dict[str, tuple[str, str]] = {
    "harvest-market": ("Harvest Market", "groceries"),
    "green-basket": ("Green Basket Grocers", "groceries"),
    "corner-pantry": ("Corner Pantry", "groceries"),
    "lantern-noodle": ("Lantern Noodle House", "dining"),
    "blue-plate": ("Blue Plate Diner", "dining"),
    "saffron-kitchen": ("Saffron Kitchen", "dining"),
    "daily-grind": ("Daily Grind Coffee", "coffee"),
    "bean-and-leaf": ("Bean & Leaf", "coffee"),
    "metrolink": ("MetroLink Transit", "transport"),
    "cityhop-rides": ("CityHop Rides", "transport"),
    "fuel-stop": ("Fuel Stop", "transport"),
    "skybridge-air": ("SkyBridge Airlines", "travel"),
    "parkside-apts": ("Parkside Apartments", "housing"),
    "harborview-hotel": ("Harborview Hotel", "travel"),
    "brightline-electric": ("Brightline Electric", "utilities"),
    "clearwave-internet": ("Clearwave Internet", "utilities"),
    "pulse-mobile": ("Pulse Mobile", "utilities"),
    "streambox": ("StreamBox", "subscriptions"),
    "tunewave": ("Tunewave Music", "subscriptions"),
    "cloudvault": ("CloudVault Storage", "subscriptions"),
    "maple-books": ("Maple Street Books", "shopping"),
    "urban-threads": ("Urban Threads", "shopping"),
    "homecraft": ("HomeCraft Supply", "shopping"),
    "starlight-cinema": ("Starlight Cinema", "entertainment"),
    "riverside-bowl": ("Riverside Bowling", "entertainment"),
    "wellspring-pharmacy": ("Wellspring Pharmacy", "health"),
    "peak-fitness": ("Peak Fitness", "health"),
}


@dataclass(frozen=True)
class Habit:
    """Discretionary spending at a set of merchants: about `per_week` purchases a week,
    each a whole-dollar amount in [low, high]. `card_share` of them go on the credit
    card, the rest are debit purchases from checking."""

    merchants: tuple[str, ...]
    per_week: float
    low: int
    high: int
    card_share: float


@dataclass(frozen=True)
class Income:
    description: str
    amount: int
    days_of_month: tuple[int, ...]  # paid on these days (a later day if the month is short)


@dataclass(frozen=True)
class MonthlyBill:
    """A bill paid from checking on `day` each month. Past bills are `completed` and
    paid by a purchase at the biller (same date and amount); the next one after the
    anchor date is `pending` and moves no money (D48)."""

    merchant: str
    nickname: str
    day: int
    low: int
    high: int  # fixed bills have low == high


@dataclass(frozen=True)
class Subscription:
    merchant: str
    amount: int
    day: int  # charged to the credit card on this day each month


@dataclass(frozen=True)
class Persona:
    key: str
    kind: str  # "student" | "professional" | "traveller"
    first_name: str
    last_name: str
    opening_checking: int
    opening_savings: int
    income: tuple[Income, ...]
    bills: tuple[MonthlyBill, ...]
    subscriptions: tuple[Subscription, ...]
    habits: tuple[Habit, ...]
    savings_transfer: int  # moved from checking to savings on the 25th of each month
    atm_per_month: float
    atm_amounts: tuple[int, ...]
    # (merchant, nickname, amount, day of month); every bill needs a day (D49)
    cancelled_bill: tuple[str, str, int, int] | None = None


PERSONAS: tuple[Persona, ...] = (
    Persona(
        key="priya",
        kind="student",
        first_name="Priya",
        last_name="Nair",
        opening_checking=900,
        opening_savings=1_200,
        income=(
            Income("Campus Library payroll", 520, (5, 19)),
            Income("Family support", 450, (1,)),
        ),
        bills=(
            MonthlyBill("parkside-apts", "rent", 1, 750, 750),
            MonthlyBill("pulse-mobile", "phone", 12, 35, 35),
            MonthlyBill("brightline-electric", "electric", 18, 28, 46),
        ),
        subscriptions=(Subscription("streambox", 9, 7), Subscription("tunewave", 6, 14)),
        habits=(
            Habit(("harvest-market", "corner-pantry"), 1.5, 18, 55, 0.4),
            Habit(("daily-grind", "bean-and-leaf"), 3.0, 4, 7, 0.6),
            Habit(("lantern-noodle", "blue-plate"), 1.2, 11, 24, 0.7),
            Habit(("metrolink",), 2.5, 3, 3, 0.0),
            Habit(("maple-books", "urban-threads"), 0.5, 12, 60, 0.8),
            Habit(("starlight-cinema", "riverside-bowl"), 0.4, 12, 22, 0.8),
            Habit(("wellspring-pharmacy",), 0.2, 8, 25, 0.5),
        ),
        savings_transfer=50,
        atm_per_month=1.0,
        atm_amounts=(20, 40, 60),
        cancelled_bill=("peak-fitness", "gym", 25, 5),
    ),
    Persona(
        key="marcus",
        kind="professional",
        first_name="Marcus",
        last_name="Webb",
        opening_checking=4_200,
        opening_savings=12_500,
        income=(Income("Northgate Labs salary", 2_650, (1, 15)),),
        bills=(
            MonthlyBill("parkside-apts", "rent", 1, 1_850, 1_850),
            MonthlyBill("clearwave-internet", "internet", 9, 65, 65),
            MonthlyBill("pulse-mobile", "phone", 12, 70, 70),
            MonthlyBill("brightline-electric", "electric", 18, 60, 115),
        ),
        subscriptions=(
            Subscription("streambox", 16, 7),
            Subscription("tunewave", 11, 14),
            Subscription("cloudvault", 3, 21),
        ),
        habits=(
            Habit(("harvest-market", "green-basket"), 1.8, 35, 140, 0.8),
            Habit(("daily-grind",), 2.0, 5, 8, 0.9),
            Habit(("saffron-kitchen", "lantern-noodle", "blue-plate"), 2.0, 22, 85, 0.9),
            Habit(("fuel-stop", "cityhop-rides"), 1.5, 14, 55, 0.9),
            Habit(("urban-threads", "homecraft"), 0.7, 25, 180, 0.95),
            Habit(("starlight-cinema",), 0.4, 15, 40, 0.9),
            Habit(("wellspring-pharmacy", "peak-fitness"), 0.5, 15, 45, 0.9),
        ),
        savings_transfer=600,
        atm_per_month=0.5,
        atm_amounts=(60, 100),
    ),
    Persona(
        key="elena",
        kind="traveller",
        first_name="Elena",
        last_name="Rossi",
        opening_checking=3_100,
        opening_savings=6_800,
        income=(Income("Rossi Design invoice", 4_300, (3,)),),
        bills=(
            MonthlyBill("parkside-apts", "rent", 1, 1_400, 1_400),
            MonthlyBill("pulse-mobile", "phone", 12, 55, 55),
            MonthlyBill("clearwave-internet", "internet", 9, 50, 50),
        ),
        subscriptions=(Subscription("streambox", 12, 7), Subscription("cloudvault", 10, 21)),
        habits=(
            Habit(("harvest-market", "green-basket"), 1.0, 25, 90, 0.7),
            Habit(("bean-and-leaf", "daily-grind"), 2.5, 4, 9, 0.8),
            Habit(("saffron-kitchen", "lantern-noodle", "blue-plate"), 2.5, 18, 95, 0.9),
            Habit(("skybridge-air",), 0.35, 140, 520, 1.0),
            Habit(("harborview-hotel",), 0.35, 120, 380, 1.0),
            Habit(("cityhop-rides", "metrolink"), 2.0, 3, 38, 0.9),
            Habit(("urban-threads", "maple-books"), 0.5, 20, 120, 0.9),
            Habit(("wellspring-pharmacy",), 0.3, 10, 35, 0.6),
        ),
        savings_transfer=400,
        atm_per_month=1.5,
        atm_amounts=(40, 80, 120),
    ),
)
