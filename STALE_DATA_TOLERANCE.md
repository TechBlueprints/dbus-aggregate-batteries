# Stale Data Tolerance Feature

## Problem
If one of the aggregated batteries disconnects or goes idle (e.g., Bluetooth connection drops, BMS temporarily unresponsive), the aggregate service would restart after `READ_TRIALS` consecutive failures. This causes the entire battery system to appear offline, even though the other batteries are functioning normally.

## Solution
The stale data tolerance feature allows the aggregate to continue operating using the last known good data from a temporarily disconnected battery, preventing unnecessary restarts.

## Configuration

Add these settings to your `config.ini`:

```ini
[DEFAULT]
# Enable stale data tolerance
USE_STALE_DATA = True

# Maximum time (in minutes) to tolerate a disconnected battery
# After this timeout, the aggregate will restart
STALE_DATA_TIMEOUT_MINUTES = 5
```

## How It Works

1. **Initial Startup**: 
   - The aggregate must successfully read from ALL batteries at least once
   - Stale data tolerance is NOT active until valid data has been cached
   - If a battery fails on initial startup, the normal `READ_TRIALS` retry logic applies
   - This ensures the system doesn't start with invalid/missing data

2. **Normal Operation**: When all batteries are responding, the aggregate updates a timestamp for each battery after every successful read.

3. **Battery Disconnect**: If a battery fails to respond (after having successfully connected at least once):
   - The aggregate checks if stale data tolerance is enabled (`USE_STALE_DATA = True`)
   - It calculates how long the battery has been disconnected
   - If within the timeout period, it logs a warning and continues operating
   - The aggregate uses the previously aggregated values (naturally becomes "stale" over time)

4. **Timeout Exceeded**: If a battery remains disconnected longer than `STALE_DATA_TIMEOUT_MINUTES`:
   - The aggregate will restart as before (after `READ_TRIALS` failures)
   - This ensures the system doesn't run indefinitely with bad data

5. **Battery Reconnects**: When a battery reconnects, it immediately starts using fresh data again

## Logging

When using stale data, you'll see different messages depending on the situation:

**Battery temporarily disconnected (within timeout):**
```
WARNING Battery 'Battery1' read failed but within stale data timeout (45s / 5min). Continuing with potentially stale data.
```

**Battery disconnected too long (exceeded timeout):**
```
ERROR Battery 'Battery1' has been disconnected for 325s, exceeding stale data timeout (5min). Will restart after 10 trials.
```

**Initial startup (no valid data yet):**
```
DEBUG Battery 'Battery1' read failed but no valid data history yet. Cannot use stale data on initial startup.
```

## Recommended Settings

- **Default (Conservative)**: `USE_STALE_DATA = False` - Original behavior, restart on any battery failure
- **BLE/Bluetooth Batteries**: `STALE_DATA_TIMEOUT_MINUTES = 3-5` - Handle brief connection drops
- **Stable Serial Batteries**: `USE_STALE_DATA = False` - If failures are rare, immediate restart is better
- **Testing**: `STALE_DATA_TIMEOUT_MINUTES = 1` - Short timeout for initial testing

## Important Notes

- **Initial Startup**: Stale data tolerance only works AFTER the aggregate has successfully read from all batteries at least once. On initial startup, if a battery is not responding, the system will retry and restart as normal.
- **Stale Data**: The aggregate continues to report values, but they won't update for the disconnected battery. The other batteries' data continues to update normally.
- **Aggregated Values**: Voltage, current, SoC, etc. are based on ALL batteries, so one battery's stale data affects the aggregate less as more batteries are in the system.
- **Safety**: The `STALE_DATA_TIMEOUT_MINUTES` ensures the system doesn't run indefinitely with bad data.
- **Reconnection**: When a battery reconnects, it immediately starts using fresh data again.

## Use Cases

1. **Bluetooth BMS with intermittent connection**: Tolerates brief disconnections without shutting down
2. **Battery maintenance**: Allows temporary disconnection of one battery without affecting the aggregate
3. **Debugging**: Continue operation while investigating why a battery is not responding

## Technical Details

The implementation is minimal and non-invasive:
- Tracks `_battery_last_valid_time` for each battery
- Updates timestamps after successful reads
- Checks timeout in the exception handler before restarting
- Resets `_readTrials` counter when within tolerance
