# SUMO London network technical audit

Source: uploaded `london_filtered.net.xml` (netconvert 1.28.0).

- SHA256: `2f50a8a2954cf936bed9521903c11de86186850f14b519f33cb7f05b53bb786c`
- Non-internal road edges: **733**; lanes: **967**
- Physical junctions: **299**, including **46** traffic-light junctions.
- Non-internal road classes: {'highway.tertiary': 158, 'highway.residential': 361, 'highway.secondary': 210, 'highway.unclassified': 2, 'highway.secondary_link': 2}
- Lane speeds (km/h): {40: 16, 50: 786, 60: 10, 80: 155}
- Bounding rectangle (SUMO xy meters): `0.00,0.00,4329.02,3269.19`
- Bounding rectangle (original lon/lat): `-81.328417,42.981366,-81.235377,43.022736`
- No edges of type `highway.service` found.

## Action needed before speeding metrics are interpreted
**153 tertiary-road lanes have speed 80 km/h.** These may be valid, but could reflect imported/default speeds. Do not automatically change them to 50; compare with OSM speed tags or another authoritative speed-limit source. Preserve corrected-road provenance (original and final values, source, decision).

## Simulation baseline (from user-reported Windows run)
- Original route vehicles: 300; inserted: 300; tripinfo records: 300; running at end: 0.
- End time: 3600 simulated seconds.
- SUMO average route length 2301.59 m; duration 238.42 s.

The baseline numbers were reported by the user; only network XML statistics were computed from the uploaded file here.
