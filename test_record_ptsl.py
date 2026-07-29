import ptsl
import time

def test_record():
    print("Verbinde...")
    try:
        with ptsl.open_engine(company_name="MyCompany", application_name="Test") as engine:
            print("Verbunden. Status:", engine.transport_state())
            
            # Arm transport
            print("Arming Transport...")
            engine.toggle_record_enable()
            time.sleep(0.5)
            
            # Start Playback
            print("Starting Playback...")
            engine.toggle_play_state()
            time.sleep(2)
            
            print("Status:", engine.transport_state())
            
            # Stop
            print("Stopping...")
            engine.toggle_play_state()
            
    except Exception as e:
        print("Fehler:", e)

if __name__ == "__main__":
    test_record()
