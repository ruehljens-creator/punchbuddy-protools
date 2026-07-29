import ptsl
with ptsl.open_engine(company_name="MyCompany", application_name="Test") as engine:
    in_time, out_time = engine.get_timeline_selection()
    print(f"In: {in_time}, Out: {out_time}")
    # Clear out point
    engine.set_timeline_selection(in_time=in_time, out_time=in_time)
    print("Out point cleared.")
