# Identifiers

```
JO-AM-HSP-00042        facility     country · governorate · type · sequence
JO-PRC-000317          practitioner
JO-ROL-0001288         role (practitioner × facility)
```

| segment | values |
|---|---|
| `JO` | ISO 3166-1 alpha-2 |
| `AM` | ISO 3166-2:JO subdivision — AM Amman, BA Balqa, AZ Zarqa, MD Madaba, IR Irbid, MA Mafraq, JA Jerash, AJ Ajloun, KA Karak, AT Tafilah, MN Ma'an, AQ Aqaba; `XX` when unknown at assignment |
| `HSP` | `facility_types.csv:id_segment` — HSP, CLN, PHC, DOC, DEN, PHR, LAB, IMG, BLB, REH, OPT, DIA, EMS, OTH |
| `00042` | sequence within (governorate, type) |

## Rules

1. **Immutable.** An ID describes the facility *as it was when first registered*. If a clinic moves to
   another governorate or becomes a hospital, it keeps its ID and the record's fields change. Never
   parse meaning out of an ID in application code — read `type` and `address.governorate`.
2. **Never reused.** A closed facility keeps its ID with `status: permanently_closed`.
3. **Merges leave tombstones.** When two records turn out to be one place, the older ID survives and
   the other becomes `status: merged, replaced_by: <survivor>`. Follow `replaced_by` if you stored it.
4. **Reserved ranges.** Sequences ≥ 90000 (facilities), ≥ 900000 (practitioners) and ≥ 9000000 (roles)
   are allocated only by the API for records users create, so the pipeline and the API can never
   collide.
5. **Stable across rebuilds.** Each record carries an `identifiers` crosswalk (OSM element, Wikidata
   QID, ministry reference …). The merge looks those up in the previous release before allocating,
   so re-running the pipeline never renumbers existing records.
