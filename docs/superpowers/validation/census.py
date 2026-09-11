"""Census of expected session hours without an H1 bar, per symbol.
Usage: python census.py <compiled_dir>"""
import sys, glob, os
import pandas as pd

SUNDAY_FIRST_HOUR = 20

def expected(first, last):
    idx = pd.date_range(first.floor("h"), last.floor("h"), freq="h")
    wd, hr = idx.dayofweek, idx.hour
    return idx[(wd <= 4) | ((wd == 6) & (hr >= SUNDAY_FIRST_HOUR))]

def main(compiled):
    print(f"| Symbol | H1 bars | Sunday bars | expected hours | missing | isolated 1h gaps |")
    print(f"|---|---|---|---|---|---|")
    for path in sorted(glob.glob(os.path.join(compiled, "*_H1.csv"))):
        sym = os.path.basename(path)[:-7]
        h1 = pd.read_csv(path, usecols=["time"], parse_dates=["time"])
        have = set(h1["time"])
        exp = expected(h1["time"].min(), h1["time"].max())
        missing = [t for t in exp if t not in have]
        singles = sum(1 for i, t in enumerate(missing)
                      if (i == 0 or missing[i-1] != t - pd.Timedelta(hours=1))
                      and (i == len(missing)-1 or missing[i+1] != t + pd.Timedelta(hours=1)))
        sundays = int((h1["time"].dt.dayofweek == 6).sum())
        print(f"| {sym} | {len(h1):,} | {sundays:,} | {len(exp):,} | {len(missing):,} | {singles} |")

if __name__ == "__main__":
    main(sys.argv[1])
