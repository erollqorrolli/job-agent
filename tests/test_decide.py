from types import SimpleNamespace as N
from jobagent import fields


def ctx(db, profile):
    return N(profile=profile, db=db, resume_path=None, answer_fn=lambda *a: None, job_id="j")


def d(db, profile, label, kind="combobox", opts=("Yes", "No"), required=True):
    return fields.decide({"kind": kind, "label": label, "options": list(opts), "required": required, "id": "x"}, ctx(db, profile))


def test_confirmed_yes_no_facts(db, profile):
    profile["preferences"].update(has_relative_at_company=False, government_employee=False, over_18=True, has_other_offers=False,
                                  willing_onsite=True, has_prior_experience=True, willing_to_relocate=True, current_employer="GRASP Lab")
    no, yes = 1, 0
    assert d(db, profile, "Do you have any relatives that are currently employed by GCM Grosvenor? *").value == no
    assert d(db, profile, "Do you have a family member that is currently employed with CesiumAstro?", "radio").value == no
    assert d(db, profile, "Are you a government employee or official?").value == no
    assert d(db, profile, "Are you at least 18 years old? *").value == yes
    assert d(db, profile, "Do you currently have any offers? *").value == no
    assert d(db, profile, "Are you open to working 4 days onsite in our NYC office? *").value == yes
    assert d(db, profile, "Have you previously completed at least 1 internship or have relevant full-time experience? *").value == yes
    assert d(db, profile, "Are you willing to relocate to Houston?").value == yes
    assert d(db, profile, "Current employer", "text").value == "GRASP Lab"


def test_us_person_and_criminal_still_ask(db, profile):
    assert d(db, profile, "Are you a U.S. Person, defined as a U.S. Citizen, a lawful permanent resident?").need
    assert d(db, profile, "Have you ever been convicted of a felony?").need


def test_unset_facts_ask_instead_of_guessing(db, profile):
    assert d(db, profile, "Are you at least 18 years old? *").need


def test_terms_and_full_duration(db, profile):
    profile["preferences"].update(accept_standard_terms=True, available_full_duration=True)
    c = fields.decide({"kind": "checkbox", "label": "I agree with the above obligations related to protection of Proprietary Information.", "id": "x", "ids": ["x"], "options": [""]}, ctx(db, profile))
    assert c.value is True
    assert d(db, profile, "Privacy Statement*", opts=("I acknowledge and agree", "I do not agree")).value == 0
    assert d(db, profile, "Can you commit to working 40 hours per week for the full duration?").value == 0


def test_gpa_only_when_required_and_range_pick(db, profile):
    profile["education"]["gpa"] = 3.3
    assert d(db, profile, "GPA", "text", (), required=False).skip
    assert d(db, profile, "What is your GPA?*", "text", (), required=True).value == "3.3"
    r = d(db, profile, "What is your current overall GPA? *", "select", ("Below 3.0", "3.0 - 3.49", "3.5+"))
    assert r.value == 1
    profile["education"]["gpa"] = 3.7
    assert d(db, profile, "What is your current overall GPA? *", "select", ("Below 3.0", "3.0 - 3.49", "3.5+")).value == 2


def test_pronouns_and_us_person(db, profile):
    profile["identity"]["pronouns"] = "He/Him"
    assert d(db, profile, "Personal Pronouns*", "combobox", ("They/Them", "She /Her", "He/ Him")).value == 2
    profile["work_authorization"].update(us_person=False, us_citizen=False)
    assert d(db, profile, "All eligible candidates must be a U.S. Person. Are you a U.S. Person?", "radio").ineligible
    r = d(db, profile, "Are you a U.S. citizen?", "select", ("Yes", "No"), required=False)
    assert r.value == 1 and not r.ineligible
    assert d(db, profile, "Are you legally authorized to work in the United States?").value == 0


def test_saved_no_does_not_bypass_us_person_ineligibility(db, profile):
    profile["work_authorization"].update(us_person=False, us_citizen=False)
    q = "As a result of the work performed by Acme, all eligible candidates must be a U.S. Person. Are you?"
    db.save_answer(fields.norm_key(q), q, "No")
    assert d(db, profile, q, "radio").ineligible


def test_enrolled_is_always_yes(db, profile):
    profile["education"]["currently_enrolled"] = True
    r = d(db, profile, "Are you enrolled in a Bachelor's, Master's or PhD program from an accredited college or university?", opts=("Yes", "No"))
    assert r.value == 0


def test_grounded_model_answers_need_real_evidence(db, profile):
    import json
    profile["facts"]["skills"] = ["Python", "Docker"]
    c = ctx(db, profile)
    from jobagent.answers import pick_grounded
    good = lambda s, u: json.dumps({"answer": "Yes", "evidence": "skills: Python"})
    made_up = lambda s, u: json.dumps({"answer": "Yes", "evidence": "worked at Google for three years"})
    unsure = lambda s, u: json.dumps({"answer": None, "evidence": ""})
    bad_opt = lambda s, u: json.dumps({"answer": "Maybe", "evidence": "skills: Python"})
    q = "Do you have experience with Python?"
    assert pick_grounded({}, q, ["Yes", "No"], profile, chat=good) == 0
    assert pick_grounded({}, q, ["Yes", "No"], profile, chat=made_up) is None     # evidence not in the facts
    assert pick_grounded({}, q, ["Yes", "No"], profile, chat=unsure) is None
    assert pick_grounded({}, q, ["Yes", "No"], profile, chat=bad_opt) is None     # answer not one of the options
    c.choice_fn = lambda qq, opts: pick_grounded({}, qq, opts, profile, chat=good)
    r = fields.decide({"kind": "select", "label": q, "options": ["Yes", "No"], "required": True, "id": "x"}, c)
    assert r.value == 0 and db.get_answer(fields.norm_key(q)) == "Yes"             # saved so the model isn't asked twice


def test_facts_sheet_has_no_contact_details(profile):
    from jobagent.answers import facts_sheet
    sheet = facts_sheet(profile)
    assert "2155550100" not in sheet and "test@example.com" not in sheet and "resume.pdf" not in sheet


def test_counts_and_free_text_never_auto_used(db, profile):
    import json
    from jobagent.answers import pick_grounded
    profile["facts"]["experience"] = ["Penn Engineering TPM intern"]
    ok = lambda s, u: json.dumps({"answer": "2", "evidence": "experience: Penn Engineering TPM intern"})
    assert pick_grounded({}, "How many prior internships have you had?", ["0", "1", "2"], profile, chat=ok) is None
    c = ctx(db, profile)
    txt = lambda s, u: json.dumps({"answer": "Python", "evidence": "skills: Python"})
    profile["facts"]["skills"] = ["Python"]
    c.choice_fn = lambda q, o: pick_grounded({}, q, o, profile, chat=txt)
    r = fields.decide({"kind": "text", "label": "Favorite language?", "required": True, "id": "x"}, c)
    assert r.need and r.need[1] == "draft"
