# Hermes Trading — Global Reflection Memory
_This file is read by reflect.py at each reflection cycle to avoid repeating past mistakes._
_Updated automatically — do not edit manually unless correcting bad data._

## How to read this file
Each section below is one reflection event. The agent uses the last 3000 chars
of this file as context when choosing the next strategy change.

## Guidelines for future reflections
- Do NOT repeat a change that was reverted within 2 reflection cycles
- If stop_loss_pct was tightened 3 times in a row, try a different lever
- If win_rate < 30% consistently, consider loosening RSI threshold before touching position size
- If all assets show same issue simultaneously, it may be a market regime — reduce position_size_r
- FVG and order_block have high signal quality but fire rarely — do not reduce their weights lightly
- sr_zone is noisy on 5m candles — tolerance_pct adjustments have outsized effect

## Reflection History
_Entries appended automatically by reflect.py_

