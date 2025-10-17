import os
import random
import uuid
import json
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room
# NOTE: The client (v3.html) is expected to connect to the root '/' path using the Socket.IO library.

# --- Configuration ---
# Set the desired port to the environment variable $PORT if available (standard for Render/Heroku)
PORT = int(os.environ.get('PORT', 5000))

app = Flask(__name__)
# Suppress the verbose warnings from SocketIO
app.config['SECRET_KEY'] = 'your_secret_key_here'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent', logger=True, engineio_logger=True)

# --- Global Game State ---

# Main components of the game state
# GAME_STATE = { id: { name: str, balance: int, hand: list, score: int, bet: int, status: str, sid: str } }
PLAYERS = {}

# Game metadata
DECK = []
DEALER_HAND = []
DEALER_SCORE = 0
GAME_PHASE = 'betting' # Can be 'betting', 'dealing', 'player_turn', 'dealer_turn', 'results'
PLAYER_TURN_ORDER = []
CURRENT_PLAYER_INDEX = 0

# --- Game Logic Functions ---

def create_deck():
    """Creates a standard 52-card deck and shuffles it."""
    suits = ['♥', '♦', '♣', '♠']
    values = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
    new_deck = [{'value': v, 'suit': s} for s in suits for v in values]
    random.shuffle(new_deck)
    return new_deck

def calculate_score(hand):
    """Calculates the score of a hand, handling Aces (1 or 11)."""
    score = 0
    num_aces = 0
    for card in hand:
        if card['value'].isdigit():
            score += int(card['value'])
        elif card['value'] in ['J', 'Q', 'K']:
            score += 10
        elif card['value'] == 'A':
            score += 11
            num_aces += 1

    # Adjust for Aces
    while score > 21 and num_aces > 0:
        score -= 10
        num_aces -= 1
    return score

def get_game_state_for_broadcast():
    """Formats the global state for sending to all clients."""
    state = {
        'phase': GAME_PHASE,
        'players': [],
        'dealer_hand': [],
        'dealer_score': 0,
        'turn_id': None
    }

    # Determine whose turn it is
    if GAME_PHASE == 'player_turn' and PLAYER_TURN_ORDER and CURRENT_PLAYER_INDEX < len(PLAYER_TURN_ORDER):
        state['turn_id'] = PLAYER_TURN_ORDER[CURRENT_PLAYER_INDEX]

    # Dealer Hand: Hide the second card unless in the dealer_turn or results phase
    if GAME_PHASE in ['dealer_turn', 'results'] or len(DEALER_HAND) < 2:
        state['dealer_hand'] = DEALER_HAND
        state['dealer_score'] = DEALER_SCORE
    elif len(DEALER_HAND) >= 2:
        # Hide the second card
        state['dealer_hand'] = [DEALER_HAND[0], {'value': 'Hidden', 'suit': '?'}]
        state['dealer_score'] = calculate_score([DEALER_HAND[0]]) # Only count the visible card

    # Player data
    for p_id, p_data in PLAYERS.items():
        state['players'].append({
            'id': p_id,
            'name': p_data['name'],
            'balance': p_data['balance'],
            'bet': p_data['bet'],
            'hand': p_data['hand'],
            'score': p_data['score'],
            'status': p_data['status'],
            'message': p_data.get('message', '')
        })
    
    return state

def deal_card(target_hand):
    """Draws one card from the deck and adds it to the target hand."""
    if DECK:
        target_hand.append(DECK.pop(0))
        return True
    return False

def check_for_blackjack(player_id):
    """Checks if a player has Blackjack after initial deal."""
    global PLAYERS
    player = PLAYERS[player_id]
    if player['score'] == 21 and len(player['hand']) == 2:
        player['status'] = 'blackjack'
        player['message'] = 'BLACKJACK! Waiting for the results phase.'
        return True
    return False

def advance_turn():
    """Moves to the next player or the next phase (Dealer's turn)."""
    global CURRENT_PLAYER_INDEX, GAME_PHASE
    
    CURRENT_PLAYER_INDEX += 1
    
    # Skip players who are 'stood', 'bust', or 'blackjack'
    while CURRENT_PLAYER_INDEX < len(PLAYER_TURN_ORDER):
        p_id = PLAYER_TURN_ORDER[CURRENT_PLAYER_INDEX]
        if PLAYERS[p_id]['status'] == 'playing':
            PLAYERS[p_id]['message'] = 'It is your turn.'
            break
        CURRENT_PLAYER_INDEX += 1

    # If all players have finished their turns, move to dealer_turn
    if CURRENT_PLAYER_INDEX >= len(PLAYER_TURN_ORDER):
        GAME_PHASE = 'dealer_turn'
        dealer_turn()

    # Broadcast state change
    socketio.emit('game_state_update', get_game_state_for_broadcast())


def dealer_turn():
    """Executes the dealer's drawing logic."""
    global DEALER_HAND, DEALER_SCORE, DECK, GAME_PHASE

    # CRITICAL: Dealer's second card is revealed and score is updated
    DEALER_SCORE = calculate_score(DEALER_HAND)
    socketio.emit('game_state_update', get_game_state_for_broadcast()) # Reveal dealer card

    # Dealer must hit on 16 or less, and stand on 17 or more (soft or hard)
    while DEALER_SCORE < 17:
        deal_card(DEALER_HAND)
        DEALER_SCORE = calculate_score(DEALER_HAND)
        
        # In a real game, this would pause for dramatic effect. Here we update immediately.
        socketio.emit('game_state_update', get_game_state_for_broadcast())
        socketio.sleep(0.5) # Wait half a second for dramatic effect

    # Dealer's turn is over, move to results
    GAME_PHASE = 'results'
    results_phase()


def results_phase():
    """Calculates final payouts for all players."""
    global GAME_PHASE, PLAYERS, DEALER_SCORE, PLAYER_TURN_ORDER

    # CRITICAL: Fix the global scope issue here
    global CURRENT_PLAYER_INDEX, DEALER_HAND, DECK 
    
    DEALER_SCORE = calculate_score(DEALER_HAND)

    for p_id in PLAYER_TURN_ORDER:
        player = PLAYERS[p_id]
        if player['bet'] == 0:
            player['status'] = 'ready'
            player['message'] = 'You sat out this round.'
            continue

        if player['status'] == 'bust':
            player['message'] = f"Bust! Dealer wins. Lost ${player['bet']}."
        elif player['status'] == 'blackjack':
            # Blackjack payout is typically 3:2
            payout = int(player['bet'] * 2.5)
            player['balance'] += payout
            player['message'] = f"BLACKJACK! You win 3:2! Won ${payout - player['bet']}."
        elif DEALER_SCORE > 21:
            # Dealer busts, player wins 1:1
            payout = player['bet'] * 2
            player['balance'] += payout
            player['message'] = f"Dealer busts! You win 1:1! Won ${player['bet']}."
        elif player['score'] > DEALER_SCORE:
            # Player wins 1:1
            payout = player['bet'] * 2
            player['balance'] += payout
            player['message'] = f"You beat the Dealer! Won ${player['bet']}."
        elif player['score'] < DEALER_SCORE:
            # Dealer wins
            player['message'] = f"Dealer wins. Lost ${player['bet']}."
        else: # Tie (Push)
            # Player gets their bet back
            player['balance'] += player['bet']
            player['message'] = f"Push (Tie). Your bet of ${player['bet']} is returned."
        
        # Reset player state for next round
        player['status'] = 'ready'
        player['bet'] = 0

    # Reset game state for next round
    GAME_PHASE = 'betting'
    DEALER_HAND = []
    DEALER_SCORE = 0
    PLAYER_TURN_ORDER = []
    CURRENT_PLAYER_INDEX = 0
    
    # Broadcast final results
    socketio.emit('game_state_update', get_game_state_for_broadcast())

def check_all_bets_placed():
    """Checks if all players who are not sitting out have placed a bet."""
    global PLAYER_TURN_ORDER

    players_betting = [p_id for p_id, p_data in PLAYERS.items() if p_data['bet'] > 0]
    
    if players_betting:
        return True, players_betting
    return False, []

def start_round(players_betting):
    """Starts the game by dealing cards and setting the turn order."""
    # CRITICAL: Fix the global scope issue here
    global DECK, DEALER_HAND, DEALER_SCORE, GAME_PHASE, PLAYER_TURN_ORDER, CURRENT_PLAYER_INDEX, PLAYERS
    
    DECK = create_deck()
    DEALER_HAND = []
    DEALER_SCORE = 0
    PLAYER_TURN_ORDER = players_betting
    CURRENT_PLAYER_INDEX = 0
    GAME_PHASE = 'dealing'
    
    # 1. Deal two cards to each betting player
    for _ in range(2):
        for p_id in PLAYER_TURN_ORDER:
            player = PLAYERS[p_id]
            deal_card(player['hand'])
            player['score'] = calculate_score(player['hand'])
        deal_card(DEALER_HAND)
        DEALER_SCORE = calculate_score(DEALER_HAND)

    # 2. Check for initial Blackjacks and set status
    for p_id in PLAYER_TURN_ORDER:
        player = PLAYERS[p_id]
        player['status'] = 'playing'
        check_for_blackjack(p_id)
        player['message'] = 'Waiting for your turn.'

    # 3. Determine the start of the first player's turn
    GAME_PHASE = 'player_turn'
    
    # Advance to the first player who is not a 'blackjack'
    advance_turn() 
    
    # If the first player is already the current player, set their message
    if PLAYER_TURN_ORDER and CURRENT_PLAYER_INDEX < len(PLAYER_TURN_ORDER):
        p_id = PLAYER_TURN_ORDER[CURRENT_PLAYER_INDEX]
        if PLAYERS[p_id]['status'] == 'playing':
            PLAYERS[p_id]['message'] = 'It is your turn. Hit or Stand?'

    # Broadcast initial deal state (dealer card hidden)
    socketio.emit('game_state_update', get_game_state_for_broadcast())

# --- SocketIO Handlers ---

@app.route('/')
def index():
    """Root route for standard HTTP check (optional)."""
    return "Blackjack Server is running."

@socketio.on('connect')
def handle_connect():
    """Handles new client connections."""
    client_sid = request.sid
    player_id = str(uuid.uuid4())
    
    # Initialize player data
    PLAYERS[player_id] = {
        'id': player_id,
        'name': f"Player {len(PLAYERS)}",
        'balance': 1000,
        'hand': [],
        'score': 0,
        'bet': 0,
        'status': 'ready', # ready, playing, bust, stood, blackjack
        'sid': client_sid
    }
    
    # Send the player their unique ID and name
    emit('player_init', {'id': player_id, 'name': PLAYERS[player_id]['name']})
    print(f"Player connected: {player_id}")
    
    # Broadcast the updated game state to everyone
    socketio.emit('game_state_update', get_game_state_for_broadcast())

@socketio.on('disconnect')
def handle_disconnect():
    """Handles client disconnections."""
    global PLAYERS
    client_sid = request.sid
    
    # Find the player ID using the session ID
    player_id_to_remove = None
    for p_id, p_data in PLAYERS.items():
        if p_data['sid'] == client_sid:
            player_id_to_remove = p_id
            break
            
    if player_id_to_remove:
        del PLAYERS[player_id_to_remove]
        print(f"Player disconnected: {player_id_to_remove}")
        
    # Re-evaluate turn order and game phase if the current player left
    # (Simplified: just advance turn if the active player disconnected)
    global GAME_PHASE
    if GAME_PHASE == 'player_turn' and PLAYER_TURN_ORDER and CURRENT_PLAYER_INDEX < len(PLAYER_TURN_ORDER):
        if PLAYER_TURN_ORDER[CURRENT_PLAYER_INDEX] == player_id_to_remove:
            advance_turn()
            
    socketio.emit('game_state_update', get_game_state_for_broadcast())

@socketio.on('place_bet')
def handle_place_bet(data):
    """Handles a player placing a bet."""
    global PLAYERS, GAME_PHASE
    player_id = None
    for p_id, p_data in PLAYERS.items():
        if p_data['sid'] == request.sid:
            player_id = p_id
            break
    
    if not player_id or GAME_PHASE != 'betting':
        # Emit an error back to the client if they bet at the wrong time
        emit('error', {'message': 'Cannot place bet outside of the betting phase.'})
        return

    amount = int(data.get('amount', 0))
    if amount <= 0: return

    player = PLAYERS[player_id]
    
    # Check if player has enough balance
    if player['balance'] >= player['bet'] + amount:
        player['bet'] += amount
        player['balance'] -= amount
        
        # Check if all necessary players have bet to start the round
        all_bet_placed, players_betting = check_all_bets_placed()
        if all_bet_placed:
            start_round(players_betting)
            return # start_round handles the broadcast
            
        socketio.emit('game_state_update', get_game_state_for_broadcast())

@socketio.on('player_hit')
def handle_player_hit():
    """Handles a player requesting another card."""
    global PLAYERS, GAME_PHASE
    player_id = None
    
    for p_id, p_data in PLAYERS.items():
        if p_data['sid'] == request.sid:
            player_id = p_id
            break

    if not player_id or GAME_PHASE != 'player_turn' or player_id != PLAYER_TURN_ORDER[CURRENT_PLAYER_INDEX]:
        emit('error', {'message': 'It is not your turn or the game is not in the player phase.'})
        return

    player = PLAYERS[player_id]
    
    if player['status'] == 'playing':
        deal_card(player['hand'])
        player['score'] = calculate_score(player['hand'])
        player['message'] = ''
        
        if player['score'] > 21:
            player['status'] = 'bust'
            player['message'] = 'BUST! You went over 21.'
            advance_turn()
        elif player['score'] == 21:
            player['status'] = 'stood' # Auto-stand on 21
            player['message'] = '21! Standing.'
            advance_turn()
        else:
            # If still playing, broadcast and wait for next action
            socketio.emit('game_state_update', get_game_state_for_broadcast())
    
@socketio.on('player_stand')
def handle_player_stand():
    """Handles a player choosing to stand."""
    global PLAYERS, GAME_PHASE
    player_id = None
    
    for p_id, p_data in PLAYERS.items():
        if p_data['sid'] == request.sid:
            player_id = p_id
            break

    if not player_id or GAME_PHASE != 'player_turn' or player_id != PLAYER_TURN_ORDER[CURRENT_PLAYER_INDEX]:
        emit('error', {'message': 'It is not your turn or the game is not in the player phase.'})
        return

    player = PLAYERS[player_id]
    
    if player['status'] == 'playing':
        player['status'] = 'stood'
        player['message'] = 'Stood on ' + str(player['score'])
        advance_turn()


# --- Server Start ---

if __name__ == '__main__':
    print(f"Starting server on port {PORT}...")
    # Using gevent to handle concurrent WebSocket connections
    socketio.run(app, host='0.0.0.0', port=PORT)