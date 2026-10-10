# Log 994–1002: destination judgment and repeated speech

Observed: cloud Google already interpreted turns; duplicate map records and fixed post-search confirmation caused repetition. Log 1001 returned the same station near-identically twice; log 997/998 attempted start before place selection completed.

Changes: deduplicate compatible near-identical POIs within 40 m; bounded model decision over real candidates (propose preview versus clarify); preserve route-start consent; recover unique unselected place from start proposal with valid plan ID/revision; record selection decision in checkpoint/history. Model timeout/output errors preserve clarification. Full vendor names remain in data, speech uses concise labels.

Validation: 74 focused backend tests passed. Expanded decision suite: 10 tests, including malformed/invented model output, ambiguity, duplicate POIs, separate entrances, stale revision and no driving before route consent. Static Ruff and diff checks passed. No live cloud/local/VietMap test or backend restart in this turn. Added model stage has a 3-second timeout; real-world selection accuracy and added latency still need live feedback.
