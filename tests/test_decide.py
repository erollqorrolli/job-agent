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


def test_referrals_and_prior_employment_default_to_no(db, profile):
    assert d(db, profile, "Were you referred by a current employee?", "radio").value == 1
    assert d(db, profile, "Do you have an internal referral?", "select").value == 1
    assert d(db, profile, "Have you worked here before?", "btngroup").value == 1
    assert d(db, profile, "Have you ever been employed with Acme?", "radio").value == 1
    referral_name = d(db, profile, "Employee referral name", "text", (), required=False)
    assert referral_name.skip and referral_name.value is None


def test_salary_expectation_uses_posted_midpoint_and_never_guesses(db, profile):
    c = ctx(db, profile)
    c.job_text = "The compensation range for this internship is $24.00 - $36.00 per hour."
    hourly = fields.decide({"kind": "text", "label": "Desired hourly rate", "options": [],
                            "required": True, "inputType": "number", "id": "x"}, c)
    assert hourly.value == "30" and hourly.source == "job-posting-pay"

    c.job_text = "The annual salary range is $80,000 to $100,000."
    annual = fields.decide({"kind": "text", "label": "Salary expectation", "options": [],
                            "required": True, "inputType": "text", "id": "x"}, c)
    assert annual.value == "$90,000 per year"

    c.job_text = "Competitive compensation is offered."
    unknown = fields.decide({"kind": "text", "label": "Salary expectation", "options": [],
                             "required": True, "inputType": "text", "id": "x"}, c)
    assert unknown.need


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


def test_saved_demographics_or_decline_are_used(db, profile):
    profile["demographics"] = {"default": "self_identify", "gender": "Man", "race": "White"}
    assert d(db, profile, "Gender", opts=("Woman", "Man", "Prefer not to say")).value == 1
    assert d(db, profile, "Race", opts=("Asian", "White", "Decline to self-identify")).value == 1
    profile["demographics"]["default"] = "decline"
    assert d(db, profile, "Race", opts=("Asian", "White", "Decline to self-identify")).value == 2


def test_hispanic_latino_profile_answers_equivalent_yes_no_eeo_question(db, profile):
    profile["demographics"] = {"default": "self_identify", "ethnicity": "Hispanic/Latino"}
    assert d(db, profile, "Are you Hispanic or Latino?", "select", ("Yes", "No"), required=False).value == 0
    assert d(db, profile, "Do you identify as Hispanic or Latinx?", "btngroup", ("No", "Yes")).value == 1
    checkbox = fields.decide({"kind": "checkbox", "label": "Hispanic or Latino", "ids": ["x"],
                              "options": ["Hispanic or Latino"], "required": False, "id": "x"},
                             ctx(db, profile))
    assert checkbox.value is True and checkbox.source == "profile-eeo"


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


def test_generic_grounded_reasoner_can_use_semantic_preflight_equivalents(db, profile):
    from jobagent.answers import pick_grounded

    profile["demographics"] = {"default": "self_identify", "ethnicity": "Hispanic/Latino"}
    question = "Do you consider yourself part of the Latin American community?"
    options = ("Yes", "No", "Prefer not to answer")

    def reasoner(_system, user):
        assert "demographics ethnicity: Hispanic/Latino" in user
        return '{"answer":"Yes","evidence":"demographics ethnicity: Hispanic/Latino"}'

    c = ctx(db, profile)
    c.choice_fn = lambda q, opts: pick_grounded({}, q, opts, profile, chat=reasoner)
    result = fields.decide({"kind": "select", "label": question, "options": list(options),
                            "required": False, "id": "x"}, c)

    assert result.value == 0 and result.source == "llm-grounded"


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


def test_optional_essay_is_still_sent_for_a_reviewable_draft(db, profile):
    profile["preferences"]["trust_llm_answers"] = False
    c = ctx(db, profile)
    c.answer_fn = lambda *args: "I built a scheduling tool and learned how to turn feedback into simpler workflows."
    r = fields.decide({"kind": "textarea", "label": "Anything else you would like us to know?",
                       "required": False, "id": "x"}, c)
    assert r.need[1] == "draft" and "scheduling tool" in r.need[2]


def test_graduation_answers_are_derived_from_preflight_date(db, profile):
    profile["education"]["graduation_date"] = "2028-05"
    assert d(db, profile, "When do you graduate?", "text", ()).value == "Spring 2028"
    assert d(db, profile, "Expected graduation term", "select",
             ("Fall 2027", "Spring 2028", "Fall 2028")).value == 1
    assert d(db, profile, "Graduation semester", "select",
             ("Fall 27", "Spring 28", "Fall 28")).value == 1
    assert d(db, profile, "Graduation year", "text", ()).value == "2028"


def test_model_facts_include_safe_graduation_derivations(profile):
    from jobagent.answers import facts_sheet
    profile["education"]["graduation_date"] = "2028-05"
    sheet = facts_sheet(profile)
    assert "graduation term: Spring 2028" in sheet
    assert "graduation month: May" in sheet
    assert "graduation year: 2028" in sheet


def test_local_model_reuses_high_confidence_question_variant(db, profile):
    import json
    from jobagent.answers import semantic_question_match

    old = "Are you willing to work from the office three days per week?"
    new = "Would you be willing to work in the office three days a week?"
    db.save_answer(fields.norm_key(old), old, "Yes")
    chat = lambda *_: json.dumps({"candidate_id": 0, "equivalent": True,
                                  "confidence": "high", "reason": "same onsite schedule question"})
    match = semantic_question_match({}, db, new, "select", ["Yes", "No"], chat=chat)
    assert match["answer"] == "Yes" and match["qkey"] == fields.norm_key(old)

    c = ctx(db, profile)
    c.question_keys = {}
    c.reuse_fn = lambda q, kind, opts: semantic_question_match({}, db, q, kind, opts, chat=chat)
    result = fields.decide({"kind": "select", "label": new, "options": ["Yes", "No"],
                            "required": True, "id": "x"}, c)
    assert result.value == 0 and result.source == "library-semantic"
    assert db.get_answer(fields.norm_key(new)) == "Yes"


def test_local_model_groups_equivalent_unresolved_questions(db, profile):
    import json
    from jobagent.answers import semantic_question_match

    old = "Are you available to begin work in June?"
    new = "Would you be available to start working in June?"
    old_key = fields.norm_key(old)
    db.add_need("old-job", old_key, old, "select", ["Yes", "No"])
    chat = lambda *_: json.dumps({"candidate_id": 0, "equivalent": True,
                                  "confidence": "high", "reason": "same June availability question"})
    match = semantic_question_match({}, db, new, "select", ["Yes", "No"], chat=chat)
    assert match["qkey"] == old_key and match["answer"] is None

    c = ctx(db, profile)
    c.question_keys = {}
    c.reuse_fn = lambda q, kind, opts: semantic_question_match({}, db, q, kind, opts, chat=chat)
    result = fields.decide({"kind": "select", "label": new, "options": ["Yes", "No"],
                            "required": True, "id": "x"}, c)
    assert result.need and c.question_keys[fields.norm_key(new)] == old_key


def test_semantic_reuse_rejects_low_confidence_and_sensitive_questions(db):
    import json
    from jobagent.answers import semantic_question_match

    old = "Are you available to start in June?"
    db.save_answer(fields.norm_key(old), old, "Yes")
    low = lambda *_: json.dumps({"candidate_id": 0, "equivalent": True,
                                 "confidence": "medium", "reason": "possibly related"})
    assert semantic_question_match({}, db, "Can you start in July?", "select", ["Yes", "No"], chat=low) is None
    assert semantic_question_match({}, db, "Have you ever been convicted of a felony?",
                                   "select", ["Yes", "No"], chat=lambda *_: 1 / 0) is None


def test_one_local_model_call_finds_existing_pending_variants(db):
    import json
    from jobagent.answers import semantic_equivalent_pending_keys

    base = "Are you available to begin work in June?"
    db.add_need("j1", fields.norm_key(base), base, "select", ["Yes", "No"])
    same = "Would you be available to start working in June?"
    different = "Would you be available to start working in July?"
    db.add_need("j2", fields.norm_key(same), same, "select", ["Yes", "No"])
    db.add_need("j3", fields.norm_key(different), different, "select", ["Yes", "No"])
    chat = lambda *_: json.dumps({"candidate_ids": [0], "confidence": "high",
                                  "reason": "same June start availability"})

    keys = semantic_equivalent_pending_keys({}, db, fields.norm_key(base), base, "select",
                                            ["Yes", "No"], "Yes", chat=chat)
    assert keys == [fields.norm_key(same)]
