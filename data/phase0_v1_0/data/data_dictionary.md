# Data Dictionary

## Core identifiers
- `patientId`: Stable synthetic application identifier (`P001`–`P010`).
- FHIR Patient IDs use `patient-p001`, etc.
- Every FHIR resource carries a `synthetic` meta tag.
- No real PHI is present.

## Medication coding
- FHIR element: `MedicationRequest.medicationCodeableConcept`.
- Code system: RxNorm.
- Prototype stores ingredient-level RxCUI values for simple deterministic matching.
- Dosage is represented as human-readable `dosageInstruction.text`.
- A production design should preserve more specific clinical-drug/product concepts and normalize to ingredients for rules.

## Laboratory coding
- Potassium: LOINC `2823-3`, UCUM `mmol/L`.
- Glucose: LOINC `2345-7`, UCUM `mg/dL`.
- eGFR: LOINC `98979-8`, UCUM `mL/min/{1.73_m2}`.
- INR: LOINC `6301-6`, UCUM `{INR}`.

## Unstructured notes
- Stored as FHIR `Binary` resources with `text/plain`.
- Referenced by FHIR `DocumentReference`.
- V1 rule logic MUST NOT create findings from note text.
- Notes are available only to enrich a grounded AI explanation after deterministic findings exist.

## Output semantics
- `DetectedIssue`: one individual deterministic safety issue.
- `RiskAssessment`: optional qualitative roll-up of overall medication-safety risk.
- No probability is asserted because this prototype has no validated predictive model.
- `dataGaps`: application-level collection for missing information; it is not automatically a clinical finding.
