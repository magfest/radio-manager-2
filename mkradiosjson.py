import json

OUTPUT = {"headsets": 64, "audits": [], "radios": {}}

radios = input("radio ids")
for radio_id in radios.split(','):
    id_str = str(int(radio_id.strip()))

    OUTPUT['radios'][id_str] = {"history": [], "last_activity": 0, "checkout": {}}
    

with open('radios.json.new', 'w') as radiofile:
    json.dump(OUTPUT, radiofile)
