# Copy-and-paste AI prompt

Copy the text below into any text-capable AI assistant. Replace the final input section with your own redacted property information.

```text
Turn my supplied property information into concise, guest-safe Markdown knowledge files for a short-term-rental guest-support assistant. This should work independently of any AI vendor or property-management platform.

Use only the facts I supply. Do not invent amenities, equipment, opening hours, bin colours, waste rules, collection dates, prices, access permissions or policies. Ask me concise questions about important gaps or conflicts before finalising. If I cannot confirm a fact, omit it from guest knowledge and list it separately as an unresolved question.

Produce separate files named:
property_facts.md
address_parking.md
arrival_departure.md
wifi.md
house_rules.md
bins_recycling.md
guest_supplies.md
appliances.md
heating_hot_water.md
housekeeping.md
ring_camera.md
local_area.md

Adapt the file list to the property; remove topics that do not apply. Use plain Markdown headings and short practical instructions, with one topic per section. Label each file with its filename and place its complete contents in a separate fenced Markdown block, or create downloadable .md files if you can. Include a last-verified date only if I supply one; do not imply that you verified the property.

Keep facts consistent across files and avoid unnecessary repetition. Include the property's timezone for arrival, departure, quiet hours and collection times. State accessibility limitations precisely rather than claiming universal accessibility.

For address and parking, include only the guest-approved postal address, verified map/entrance directions, allocated parking location, vehicle capacity/size limits, costs, permits/reservations, street restrictions and EV policy. Do not publish an address reserved for confirmed bookings into knowledge used for pre-booking inquiries. Do not invent free parking or guarantee an unreserved space.

For Wi-Fi, include the guest SSID, verified service/coverage information and where guests obtain the password. Do not include actual passwords, door/access codes, key-safe combinations, API keys, router administration, private contact details, surveillance records, financial information or internal operations. Exclude home-automation integrations, Home Assistant, scripts, sensors, server/network topology and control logic. Describe only ordinary guest-facing controls.

For rubbish/trash and recycling, include indoor/outdoor bin locations; each verified label and bin/lid colour; accepted and prohibited items; rinsing, flattening and bagging requirements; weekly or alternating collection schedule; collection point/time/timezone; who puts bins out and brings them back; and what to do if bins are full or collection is missed. Colours and rules vary locally: do not infer them. Holiday changes require a current confirmed source.

For appliances, use only the exact installed model's verified guest operating instructions. Do not infer grill-door position, shower power/flow behaviour or repair/reset procedures. Faults and unsafe conditions require host review. Do not ask guests to dismantle, service or repair equipment.

For Ring doorbells or cameras, include only verified device locations, field of view, video/audio recording status, guest-facing purpose and published disclosure. Do not infer settings from brand/model or claim no other/indoor cameras without verification. Privacy, footage, deletion and disabling requests require host review. Include no credentials, recordings, other guests' information or device-disable/reset instructions.

For supplies, distinguish initial provision, guest-accessible spares and the host's replenishment policy. A request for more supplies goes to the host to arrange; do not promise a person, delivery time, purchase or fee. Restricted cupboards/garages remain restricted unless that specific booking has explicit permission. A clear confirmation that guests already have enough and need nothing else is resolved. Mixed messages still require attention to any outstanding request. Application code controls notification behaviour; text files do not turn alerts off by themselves.

Booking extensions, early/late arrival or departure, luggage storage, visitors, refunds, discounts, exceptions, repairs and any missing or conflicting facts require host confirmation. Do not turn a previous one-off concession into a standing policy.

Finish with a separate owner-only checklist of missing facts, conflicting sources, items removed for privacy and questions to test. Keep that checklist and these authoring instructions out of the guest knowledge files. Final guest files must have no unfilled placeholders or unsupported claims.

PROPERTY INPUT (redacted house guide, current policies, exact appliance instructions and local bin information):
[PASTE YOUR INFORMATION HERE]
```
