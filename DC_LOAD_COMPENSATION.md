# DC Load Compensation Feature (Parked)

## Status: Parked -- not solving the immediate problem

## What it does

When enabled, reads `/Dc/System/Power` from `com.victronenergy.system` and inflates the
published `MaxChargeCurrent` (CCL) so that DVCC allocates enough charger capacity to cover
both DC loads and battery charging.

Victron's built-in `ExtraBatteryCurrent` does the same thing but requires a dedicated
SmartShunt configured as a DC System meter (`MeasurementType = 1`). This feature uses
the calculated value instead.

## Safety checks

1. Only active when `DC_LOAD_COMPENSATION = True` in config.ini
2. Skipped when `MaxChargeCurrent <= 0` (BMS requests no charge)
3. Skipped when any battery is at 100% SoC
4. Capped at the charge deficit (what batteries request but aren't receiving) plus 10%
   of the true CCL as tolerance

## Why it's parked

Testing on the Cerbo revealed the actual problem is not DC loads stealing charge current.
The real bottleneck is:

1. **Aggressive CCL tapering in dbus-serialbattery**: Battery 1's max cell at 3.448V
   triggers the CCCM_CV taper, driving its CCL to 0A even at 97% SoC.
2. **`min * count` aggregation in dbus-aggregate-batteries**: The aggregate computes
   `CCL = min(battery_CCLs) * NR_OF_BATTERIES`. One battery at 0A CCL drags the entire
   bank to 0A, even when the other battery still requests 5.3A.

With the aggregate publishing CCL = 0A, DC load compensation can't help (the
`MaxChargeCurrent > 0` guard correctly prevents it).

## What needs to happen first

- Fix the CCL aggregation: `min(CCLs) * count` should be `sum(CCLs)` for parallel
  batteries. Each battery independently reports what it can accept.
- Possibly adjust the serialbattery CCCM_CV tapering thresholds so CCL doesn't hit 0
  before the battery reaches 100%.

Once those are resolved, DC load compensation may still be useful for systems where the
BMS requests charge but charger output is consumed by DC loads before reaching batteries.

## Files changed

- `dbusmon.py` -- added `/Dc/System/Power` to system service monitoring
- `config.default.ini` -- added `DC_LOAD_COMPENSATION = False` option
- `settings.py` -- added `DC_LOAD_COMPENSATION` config loading
- `dbus-aggregate-batteries.py` -- DC load read, safety checks, CCL inflation, logging
