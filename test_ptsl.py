import ptsl
with ptsl.open_engine(company_name="MyCompany", application_name="Test") as engine:
    print("Transport Armed:", engine.transport_armed())
    print("Transport State:", engine.transport_state())
