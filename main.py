from marlin_prober import MarlinProber

# 1. Initialize the prober (Set debug_mode=False for real CNC)
probe_session = MarlinProber(debug_mode=True)

# 2. Programmatically set the parameters
# In this example: Line along X=0 to 100, Probing in Y+ direction
probe_session.configure(
    l_ax = 'X',             # Axis of the line
    start = 0.0,            # Line start
    end = 100.0,            # Line end
    count = 10,             # Number of points
    fixed_vals = {          # Coordinates for axes NOT being "lined"
        'Y': 50.0, 
        'Z': -10.0
    },
    p_ax = 'Y',             # Axis to probe IN
    dir_pos = True,         # Direction (True = positive, False = negative)
    offset = 5.0,           # Staging offset (mm)
    probe_dist = 10.0,      # Max probe travel (mm)
    host = "192.168.1.50",  # CNC IP (ignored if debug_mode=True)
    feed_move = 2000,       # Feed rate for positioning/staging moves (mm/min)
    feed_probe = 100        # Feed rate for probing moves (mm/min)
)

# 3. Setup the UI
probe_session.setup_visualization()

# 4. Optional: Add a custom manual confirmation before hardware moves
# input("Check the plot. Press Enter to start physical probing...")

# 5. Execute
probe_session.run()

# 6. Access the results for further processing
print(f"Absolute results: {probe_session.abs_results}")
print(f"Relative deviations: {probe_session.rel_results}")