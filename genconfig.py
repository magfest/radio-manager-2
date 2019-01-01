#!/usr/bin/env python3
#{
#    "radios": [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15],
#    "departments": {
#        "TechOps": {"limit": null},
#        "Arcade": {"limit": 2},
#

import csv
import json

results = {"radios": [], "departments": {}}

with open("requests.csv", newline="") as f:
    reader = csv.reader(f)

    for row in reader:
        print(row)
        dept, count = row
        if int(count) > 0:
            results["departments"][dept] = {"limit": int(count)}

with open("requests.json", "w") as f:
    json.dump(results, f, indent=4, sort_keys=True)
