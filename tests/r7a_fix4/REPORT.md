# R7a fix round 4 tests (finding: zero trace records after reaper end_run, close, drain)

| test | base | reason |
|---|---|---|
| n1 plain driver: reaper end_run(finish_stuck=False), close, drain | RED | zero records: close sets done without a record, drain's finish returns early |
| n2 service driver, same sequence | green | cancel capability path already writes a record |
| n3 close first, then drain | green | no earlier end_run, so the drain writes the record |
| n4 end_run server-side finish, then drain | green | guard; early return avoids a duplicate |
| n5 plain mid-flight cancel | green | guard; one record |
