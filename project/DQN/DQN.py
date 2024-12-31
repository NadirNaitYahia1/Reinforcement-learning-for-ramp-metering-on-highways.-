import traci
import numpy as np
import random
import torch
import torch.nn as nn
import torch.optim as optim
import logging

# Parameters
alpha = 0.1
gamma = 0.9
epsilon = 0.3
epsilon_decay = 0.99
min_epsilon = 0.1
num_states = 6
num_actions = 3
batch_size = 32
target_update = 10
learning_rate = 0.001
maxAutoroute = 0
maxBretelle = 0

# Define the neural network model for DQN
class DQN(nn.Module):
    def __init__(self, state_size, action_size):
        super(DQN, self).__init__()
        self.fc1 = nn.Linear(state_size, 64)
        self.fc2 = nn.Linear(64, 64)
        self.fc3 = nn.Linear(64, action_size)
    
    def forward(self, state):
        x = torch.relu(self.fc1(state))
        x = torch.relu(self.fc2(x))
        return self.fc3(x)

# Experience Replay Buffer
class ReplayBuffer:
    def __init__(self, capacity):
        self.buffer = []
        self.capacity = capacity
        self.idx = 0
    
    def push(self, state, action, reward, next_state, done):
        if len(self.buffer) < self.capacity:
            self.buffer.append(None)
        self.buffer[self.idx] = (state, action, reward, next_state, done)
        self.idx = (self.idx + 1) % self.capacity
    
    def sample(self, batch_size):
        return random.sample(self.buffer, batch_size)
    
    def size(self):
        return len(self.buffer)

# Initialize the DQN and target network
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = DQN(num_states, num_actions).to(device)
target_model = DQN(num_states, num_actions).to(device)
target_model.load_state_dict(model.state_dict())
target_model.eval()

optimizer = optim.Adam(model.parameters(), lr=learning_rate)
replay_buffer = ReplayBuffer(10000)

# Functions
def categorize_state(density_bretelle, density_autoroute, thresholds):
    if density_bretelle <= thresholds['bretelle_low'] and density_autoroute <= thresholds['autoroute_low']:
        return 0
    elif density_bretelle <= thresholds['bretelle_low'] and density_autoroute > thresholds['autoroute_low']:
        return 1
    elif thresholds['bretelle_low'] < density_bretelle <= thresholds['bretelle_medium'] and density_autoroute <= thresholds['autoroute_low']:
        return 2
    elif thresholds['bretelle_low'] < density_bretelle <= thresholds['bretelle_medium'] and density_autoroute > thresholds['autoroute_low']:
        return 3
    elif density_bretelle > thresholds['bretelle_medium'] and density_autoroute <= thresholds['autoroute_low']:
        return 4
    else:
        return 5

def calculate_thresholds(time_of_day):
    if time_of_day == 'morning':
        return {'bretelle_low': 3, 'bretelle_medium': 6, 'autoroute_low': 25}
    elif time_of_day == 'midday':
        return {'bretelle_low': 2, 'bretelle_medium': 4, 'autoroute_low': 15}
    elif time_of_day == 'evening':
        return {'bretelle_low': 3, 'bretelle_medium': 5, 'autoroute_low': 20}
    else:
        return {'bretelle_low': 1, 'bretelle_medium': 2, 'autoroute_low': 5}

def get_state():
    global maxAutoroute, maxBretelle
    density_autoroute = sum(traci.lane.getLastStepVehicleNumber(f"E0_{i}") for i in range(3))
    maxAutoroute = max(density_autoroute, maxAutoroute)
    density_bretelle = traci.lane.getLastStepVehicleNumber("E2_0")
    maxBretelle = max(density_bretelle, maxBretelle)
    current_time = traci.simulation.getTime()

    if 0 <= current_time < 1800:
        time_of_day = 'morning'
    elif 1800 <= current_time < 3600:
        time_of_day = 'midday'
    else:
        time_of_day = 'evening'

    thresholds = calculate_thresholds(time_of_day)
    # Return a 1D array with exactly `num_states` elements
    return np.array([categorize_state(density_bretelle, density_autoroute, thresholds)] * num_states)

def get_action(state, epsilon):
    if random.random() < epsilon:
        return random.choice(range(num_actions))
    # Ensure the state tensor has shape (1, num_states)
    state_tensor = torch.FloatTensor(state).unsqueeze(0).to(device)  # Add batch dimension
    with torch.no_grad():
        q_values = model(state_tensor)
    return torch.argmax(q_values).item()


def train_dqn():
    if replay_buffer.size() < batch_size:
        return
    batch = replay_buffer.sample(batch_size)
    states, actions, rewards, next_states, dones = zip(*batch)
    states = torch.FloatTensor(states).to(device)
    actions = torch.LongTensor(actions).to(device)
    rewards = torch.FloatTensor(rewards).to(device)
    next_states = torch.FloatTensor(next_states).to(device)
    dones = torch.BoolTensor(dones).to(device)
    current_q_values = model(states).gather(1, actions.unsqueeze(1)).squeeze(1)
    next_q_values = target_model(next_states).max(1)[0]
    target_q_values = rewards + (1 - dones.float()) * gamma * next_q_values
    loss = nn.MSELoss()(current_q_values, target_q_values)
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

def update_target_model():
    target_model.load_state_dict(model.state_dict())

def get_reward(density_autoroute, queue_bretelle):
    throughput = sum(traci.lane.getLastStepVehicleNumber(f"E0_{i}") for i in range(3))
    collisions = traci.simulation.getCollisions()
    num_collisions = len(collisions)
    emergency_brakes = 0
    for vehicle_id in traci.vehicle.getIDList():
        if traci.vehicle.getEmergencyDecel(vehicle_id) > 0:
            emergency_brakes += 1
    reward = -(density_autoroute + queue_bretelle) + 0.1 * throughput - 10 * num_collisions - 5 * emergency_brakes
    return reward

# Simulation loop
for epoch in range(3):
    traci.start(["sumo-gui", "-c", "../simulation/sumo.sumocfg", "--start", "true", "--xml-validation", "never", "--log", "log", "--quit-on-end"])
    for _ in range(10):
        traci.simulation.step()
    state = get_state()
    done = False
    epsilon = max(min_epsilon, epsilon * epsilon_decay)

    while not done:
        action = get_action(state, epsilon)
        try:
            signal_states = ["GGGr", "GGGy", "GGGG"]
            traci.trafficlight.setRedYellowGreenState("feux", signal_states[action])
        except Exception as e:
            logging.error(f"Error setting traffic light state: {e}")
        traci.simulation.step()
        density_autoroute = sum(traci.lane.getLastStepVehicleNumber(f"E0_{i}") for i in range(3))
        queue_bretelle = traci.lane.getLastStepVehicleNumber("E2_0")
        reward = get_reward(density_autoroute, queue_bretelle)
        next_state = get_state()
        replay_buffer.push(state, action, reward, next_state, done)
        train_dqn()
        state = next_state
        done = (traci.simulation.getMinExpectedNumber() == 0)
    if epoch % target_update == 0:
        update_target_model()
    traci.close()
