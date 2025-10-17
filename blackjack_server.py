import json
import threading
import random
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import sys

# --- SERVER CONFIGURATION ---
HOST = '0.0.0.0'
PORT = 5000
app = Flask(__name__)
# Replace with a secure, long, random key for production
app.config['SECRET_KEY'] = 'a_secure_blackjack_key_12345'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# --- GAME STATE ---
# Key: session_id (SocketIO), Value: player_data {id: int, hand: list, score: int, bet: int, status: str, ...}
game_state = {}
next_player_id = 0
MAX_PLAYERS = 4
GAME_ROOM = 'main_blackjack_table'
DECK = [] # The single, central deck for the game
DEALER_HAND = []
DEALER_SCORE = 0
GAME_PHASE = 'betting' # 'betting', 'dealing', 'player_turn', 'dealer_turn', 'results'

state_lock = threading.Lock()

# --- UTILITIES ---

def get_new_player_id():
    """Assigns the next available player ID."""
    global next_player_id
    player_id = next_player_id
    next_player_id += 1
    return player_id

def create_deck():
    """Creates a standard 52-card deck (server-side)."""
    global DECK
    suits = ['♥', '♦', '♣', '♠']
    values = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    DECK = []
    for suit in suits:
        for value in values:
            DECK.append({'suit': suit, 'value': value})
    random.shuffle(DECK)

def calculate_score(cards):
    """Calculates the Blackjack score for a hand (server-side logic)."""
    score = sum([get_card_value(card) for card in cards])
    numAces = sum([1 for card in cards if card['value'] == 'A'])
    while score > 21 and numAces > 0:
        score -= 10
        numAces -= 1
    return score

def get_card_value(card):
    """Gets the numeric value of a card."""
    if card['value'] in ['J', 'Q', 'K']: return 10
    if card['value'] == 'A': return 11
    return int(card['value'])

def get_public_state():
    """
    Assembles and cleans up the current state for broadcasting to clients.
    Hides the dealer's second card before the reveal phase.
    """
    public_players = list(game_state.values())
    
    # Dealer's Hand handling
    dealer_hand_public = []
    if DEALER_HAND:
        # Always show the first card
        dealer_hand_public.append(DEALER_HAND[0])
        
        # Hide the second card unless the phase is 'dealer_turn' or 'results'
        if GAME_PHASE in ['dealer_turn', 'results']:
            dealer_hand_public.extend(DEALER_HAND[1:])
        else:
            # Placeholder for hidden card
            if len(DEALER_HAND) > 1:
                dealer_hand_public.append({'value': 'Hidden', 'suit': ''})
    
    return {
        'players': public_players,
        'dealer_hand': dealer_hand_public,
        'dealer_score': DEALER_SCORE if GAME_PHASE in ['dealer_turn', 'results'] else calculate_score(DEALER_HAND[:1]),
        'phase': GAME_PHASE,
        'next_player_id': next_player_id # useful for knowing who is next to join
    }

def broadcast_state():
    """Sends the current global game state to all connected clients."""
    state = get_public_state()
    socketio.emit('game_state_update', state, room=GAME_ROOM)

# --- CORE GAME LOGIC (Server-Side) ---

def check_player_blackjack(session_id):
    """Checks for Blackjack on initial deal."""
    player_data = game_state[session_id]
    if player_data['score'] == 21 and len(player_data['hand']) == 2:
        player_data['status'] = 'blackjack'
        player_data['message'] = 'BLACKJACK!'
        return True
    return False

def check_dealer_turn():
    """Checks if all players have stood or busted, and starts dealer's turn."""
    # Check if any player is still 'playing'
    is_anyone_playing = any(p['status'] == 'playing' for p in game_state.values())
    
    if not is_anyone_playing and len(game_state) > 0 and GAME_PHASE == 'player_turn':
        print("[GAME] Starting dealer's turn.")
        global GAME_PHASE
        GAME_PHASE = 'dealer_turn'
        dealer_play()

def dealer_play():
    """Dealer hits until score is 17 or more."""
    global DEALER_SCORE, GAME_PHASE
    
    # Calculate initial score for the dealer
    DEALER_SCORE = calculate_score(DEALER_HAND)
    
    # Use a loop to simulate dealer's play and send updates between hits
    def dealer_move_step():
        global DEALER_SCORE, GAME_PHASE
        
        if DEALER_SCORE < 17 and GAME_PHASE == 'dealer_turn':
            new_card = DECK.pop()
            DEALER_HAND.append(new_card)
            DEALER_SCORE = calculate_score(DEALER_HAND)
            
            print(f"[DEALER] Hits: {new_card['value']}{new_card['suit']}. Score: {DEALER_SCORE}")
            broadcast_state()
            
            # Schedule next move
            socketio.event_handlers[None]['dealer_move_step'] = dealer_move_step # Re-register for SocketIO context
            socketio.sleep(1) # Delay between hits
            socketio.start_background_task(target=dealer_move_step)
        else:
            # Dealer stands or busts, time for results
            GAME_PHASE = 'results'
            print("[GAME] Dealer stands. Determining winners...")
            determine_winners()
            broadcast_state()

    socketio.start_background_task(target=dealer_move_step)

def determine_winners():
    """Compares hands and updates player balances (placeholder for a real balance system)."""
    global DEALER_SCORE, GAME_PHASE
    final_dealer_score = calculate_score(DEALER_HAND)
    
    # Determine the winner for each player
    for session_id in list(game_state.keys()):
        player_data = game_state.get(session_id)
        if not player_data or player_data['status'] in ['busted', 'blackjack', 'pushed', 'won', 'lost']:
            continue
            
        player_score = player_data['score']
        bet = player_data['bet']
        message = ""
        
        if player_score > 21: # Already busted, handled during player turn
            player_data['status'] = 'busted'
            message = 'Bust! Loss.'
        elif final_dealer_score > 21:
            # Player wins 1:1 if not busted
            player_data['balance'] += bet * 2
            player_data['status'] = 'won'
            message = 'Dealer Busted! Win!'
        elif player_score > final_dealer_score:
            # Player wins 1:1
            player_data['balance'] += bet * 2
            player_data['status'] = 'won'
            message = 'Win!'
        elif player_score < final_dealer_score:
            # Player loses (bet already deducted)
            player_data['status'] = 'lost'
            message = 'Loss!'
        else: # Tie (Push)
            # Player gets bet back
            player_data['balance'] += bet
            player_data['status'] = 'pushed'
            message = 'Push (Tie)!'

        player_data['message'] = message
        player_data['bet'] = 0 # Clear bet for next round
        game_state[session_id] = player_data # Update state (though dict is mutable)

    # Set phase back to betting to signal round completion
    GAME_PHASE = 'betting'
    # Optional: Clear DECK and DEALER_HAND if starting a new shoe, but we keep it simple here.

# --- WEB SOCKET HANDLERS ---

@socketio.on('connect')
def handle_connect():
    """Handles a new player connecting via WebSocket."""
    with state_lock:
        session_id = request.sid
        player_id = get_new_player_id()
        
        # New Player Data (Balance is tracked server-side)
        player_data = {
            'id': player_id,
            'name': f'Player{player_id}',
            'balance': 2500, # Initial bankroll
            'bet': 0,
            'hand': [],
            'score': 0,
            'status': 'connected', # 'connected', 'betting', 'playing', 'stood', 'busted', 'blackjack', etc.
            'message': 'Place your bet.'
        }
        
        game_state[session_id] = player_data
        join_room(GAME_ROOM)

        print(f"[CONNECT] Player {player_id} connected (SID: {session_id}). Total: {len(game_state)}")

        # 1. Send the player their unique data
        emit('player_init', player_data)
        
        # 2. Broadcast the full current state to everyone in the room
        broadcast_state()

@socketio.on('disconnect')
def handle_disconnect():
    """Handles a player disconnecting."""
    session_id = request.sid
    
    with state_lock:
        if session_id in game_state:
            player_id = game_state[session_id]['id']
            del game_state[session_id]
            print(f"[DISCONNECT] Player {player_id} disconnected (SID: {session_id}). Remaining: {len(game_state)}")
            
            broadcast_state()
            leave_room(GAME_ROOM)
            
            # Check if game needs to transition due to player leaving
            check_dealer_turn()

@socketio.on('place_bet')
def handle_place_bet(data):
    """Handles a player placing a bet."""
    session_id = request.sid
    bet_amount = int(data.get('amount', 0))
    
    with state_lock:
        player_data = game_state.get(session_id)
        if not player_data or GAME_PHASE != 'betting':
            return
            
        if player_data['balance'] >= bet_amount and bet_amount > 0:
            player_data['balance'] -= bet_amount
            player_data['bet'] += bet_amount
            player_data['status'] = 'betting'
            player_data['message'] = f'Bet placed: ${player_data["bet"]}'
            
            print(f"[BET] {player_data['name']} bet ${player_data['bet']}")
            
            # Check if all connected players have placed a bet (simplified logic)
            all_betting = all(p.get('bet', 0) > 0 for p in game_state.values())
            
            if all_betting and len(game_state) > 0:
                deal_initial_hands()
        
        broadcast_state()


@socketio.on('deal_hand')
def deal_initial_hands():
    """Initiates a new round (called internally once all players bet)."""
    with state_lock:
        global DECK, DEALER_HAND, DEALER_SCORE, GAME_PHASE
        
        # Reset game state for the new round
        create_deck()
        DEALER_HAND = []
        DEALER_SCORE = 0
        GAME_PHASE = 'dealing'
        
        # Reset player hands and status
        for session_id in game_state.keys():
            player = game_state[session_id]
            player['hand'] = []
            player['score'] = 0
            player['status'] = 'playing'
            player['message'] = 'Your turn to play.'
            
            # Deal 2 cards to each player
            player['hand'].append(DECK.pop())
            player['hand'].append(DECK.pop())
            player['score'] = calculate_score(player['hand'])
            
            # Check for immediate Blackjack
            if check_player_blackjack(session_id):
                player['status'] = 'blackjack'
                
        # Deal 2 cards to the dealer
        DEALER_HAND.append(DECK.pop())
        DEALER_HAND.append(DECK.pop())
        DEALER_SCORE = calculate_score(DEALER_HAND) # Full score, but only 1 card shown initially

        GAME_PHASE = 'player_turn'
        
        print(f"[DEAL] Round started. Dealer shows: {DEALER_HAND[0]['value']}{DEALER_HAND[0]['suit']}")
        broadcast_state()


@socketio.on('player_hit')
def handle_player_hit():
    """Handles a player requesting another card."""
    session_id = request.sid
    with state_lock:
        player_data = game_state.get(session_id)
        if not player_data or player_data['status'] != 'playing' or GAME_PHASE != 'player_turn':
            return
            
        new_card = DECK.pop()
        player_data['hand'].append(new_card)
        player_data['score'] = calculate_score(player_data['hand'])

        if player_data['score'] > 21:
            player_data['status'] = 'busted'
            player_data['message'] = 'Busted!'
            print(f"[HIT] {player_data['name']} busts with {player_data['score']}")
            
            # Check if dealer needs to play now
            check_dealer_turn()

        broadcast_state()

@socketio.on('player_stand')
def handle_player_stand():
    """Handles a player choosing to stand."""
    session_id = request.sid
    with state_lock:
        player_data = game_state.get(session_id)
        if not player_data or player_data['status'] != 'playing' or GAME_PHASE != 'player_turn':
            return
            
        player_data['status'] = 'stood'
        player_data['message'] = 'Stood.'
        print(f"[STAND] {player_data['name']} stands with {player_data['score']}")
        
        # Check if dealer needs to play now
        check_dealer_turn()
        
        broadcast_state()

# --- FLASK ROUTE ---

@app.route('/')
def index():
    """Placeholder route to confirm server status."""
    return (
        "<div style='font-family: sans-serif; padding: 40px; text-align: center; background-color: #1a2a3a; color: #f0f0f0; min-height: 100vh;'>"
        "<h1>Blackjack Server Online</h1>"
        "<p>This Python server is running the core game logic via **WebSockets (Socket.IO)**.</p>"
        "<p>The HTML client needs to connect to this server's public URL/IP to play the game.</p>"
        "<p><strong>Next Step:</strong> Integrate Socket.IO into your HTML file to start the real-time communication.</p>"
        "</div>"
    )

# --- SERVER STARTUP ---

if __name__ == '__main__':
    print(f"--- Starting Flask-SocketIO Blackjack Server on {HOST}:{PORT} ---")
    socketio.run(app, host=HOST, port=PORT, debug=True)
