# External Sources Used to Author Phase 0

Accessed 2026-09-18.

## Medication identity
- NLM RxNorm API documentation:
  https://lhncbc.nlm.nih.gov/RxNav/APIs/api-RxNorm.findRxcuiByString.html

## Laboratory terminology
- Potassium LOINC 2823-3: https://loinc.org/2823-3
- Glucose LOINC 2345-7: https://loinc.org/2345-7
- eGFR CKD-EPI 2021 LOINC 98979-8: https://loinc.org/98979-8
- INR LOINC 6301-6: https://loinc.org/6301-6

## Drug-label evidence
- Warfarin bleeding-interaction evidence:
  https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=6b227c9e-02e9-4ec9-8b11-3ab6fe5093e5
- Lisinopril hyperkalemia:
  https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=d8a0c806-8f5e-41c0-91c6-ad267af27a53
- Lisinopril + NSAID renal-function warning:
  https://dailymed.nlm.nih.gov/dailymed/fda/fdaDrugXsl.cfm?setid=9b2c04b9-783b-416f-9a2a-179ab8f9036e&type=display
- Metformin renal impairment:
  https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=bfd6ec0f-8264-4877-aa6d-f347f53bc426
- Insulin glargine hypoglycemia:
  https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=3ac85ebb-5594-59c8-77fd-df254329d151
- Warfarin INR above 4:
  https://dailymed.nlm.nih.gov/dailymed/fda/fdaDrugXsl.cfm?setid=b1bd0d02-eccb-4449-82b9-9b552835029e&type=display
- ADA Standards of Care 2026, hypoglycemia:
  https://diabetesjournals.org/care/article/49/Supplement_1/S132/163927/6-Glycemic-Goals-Hypoglycemia-and-Hyperglycemic

## FHIR R4
- DetectedIssue: https://hl7.org/fhir/R4/detectedissue.html
- RiskAssessment: https://hl7.org/fhir/R4/riskassessment.html

## AWS HealthLake
- Importing FHIR data:
  https://docs.aws.amazon.com/healthlake/latest/devguide/importing-fhir-data.html
- Migration/import pattern and NDJSON:
  https://docs.aws.amazon.com/healthlake/latest/devguide/architectural-patterns-migration.html
- HealthLake tutorial:
  https://docs.aws.amazon.com/healthlake/latest/devguide/getting-started-tutorial.html

## Important
DailyMed has multiple manufacturer/package labels. This prototype uses representative current label text to support a small demonstration rule catalog. It is not intended to replace a commercial medication knowledge base or validated clinical decision support.
