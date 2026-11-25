#!/usr/bin/perl
# SigmaLithos - the Lithos mining pool (lithosprotocol.org) from the Ergo wallet.
# A subdirectory app of SigmaSwap (shared config, helper, venv, cache):
#   https://<host>/cgi-bin/sigmaswap/sigmalithos/
#
#   Mosaik
#     GET  api.pl                     -> start screen: wallet address, then three choices
#     POST api.pl?action=dash         -> pool dashboard (Lithos's public statistics)
#     POST api.pl?action=mine         -> what a miner address earned lately (read from the chain)
#     POST api.pl?action=trade / quote / orders / status&n=   -> buy and sell LIT on LithosDex
#     GET  api.pl?action=notify       -> wallet notification check
#   ErgoPay
#     GET  api.pl?action=ergopay&side=buy|sell&a=&m=&addr=&n=    place an order
#     GET  api.pl?action=ergopay&r=<order box>&addr=             cancel an order
#
# Trading uses LithosDex orders: the user's funds go into an order box under Lithos's own
# order contract, a Lithos miner fills it against the ERG:LIT pool at the pool's price and
# never below the user's minimum, and until then only the user's key can take it back.
# Independent of the Lithos project. Public, no login, no keys.

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/..";
use CGI ();
use JSON::PP ();
use Math::BigInt;
use File::Spec;
use AppConfig qw(cfg base_url chain);
use MosaikKit;

use constant APP_VERSION => 8;
use constant ERG         => '0' x 64;
use constant LIT         => 'c1980d829988229516430a47a5eca376060b6ce859616db0936e78ab25cb6de7';
use constant SLIPPAGE    => (50 => '0.5 %', 100 => '1 %', 300 => '3 %', 1000 => '10 %');

$CGI::POST_MAX        = 64 * 1024;
$CGI::DISABLE_UPLOADS = 1;

my $cgi = CGI->new;
print $cgi->header(-type => 'application/json', -charset => 'utf-8', -cache_control => 'no-store');

my $BASE = base_url('sigmalithos');
my $API  = "$BASE/api.pl";

my $action = $cgi->url_param('action');
$action = $cgi->param('action') unless defined $action && $action ne '';
$action = '' unless defined $action;

# Signing anything that touches a Lithos contract needs a wallet whose signing library knows Ergo 6.0:
# Ergo wallet app 2.5.1 or newer (LITHOS_MIN_WALLET, "major.minor.patch"; empty switches the check off).
# The wallet says which version it is. An older one gets the read-only screens and is told why; one
# that does not say is let through with the warning shown.
my @MIN_WALLET = (cfg('LITHOS_MIN_WALLET', '2.5.1') =~ /^(\d+)\.(\d+)(?:\.(\d+))?\z/) ? ($1, $2, $3 // 0) : ();
my $WALLET_OK  = @MIN_WALLET ? wallet_at_least(@MIN_WALLET) : 1;           # 1 yes, 0 too old, undef unknown
my $MIN_TEXT   = @MIN_WALLET ? join('.', @MIN_WALLET) : '';
my $TOO_OLD    = (defined $WALLET_OK && !$WALLET_OK) ? 1 : 0;
sub wallet_text { my $v = wallet_version(); return $v ? join('.', @$v) : 'unknown' }
sub too_old_text {
    return "This needs Ergo wallet app version $MIN_TEXT or newer. Yours is " . wallet_text()
         . ', which cannot sign transactions with Lithos\'s contracts. Update the wallet app and open SigmaLithos again.';
}

my %mosaik = ('' => \&act_app, home => \&act_home, dash => \&act_dash, mine => \&act_mine,
              trade => \&act_trade, quote => \&act_quote, orders => \&act_orders, status => \&act_status,
              liq => \&act_liq, liqq => \&act_liqq, liqpos => \&act_liqpos, lstatus => \&act_lstatus,
              lend => \&act_lend, lendq => \&act_lendq);

if ($cgi->cgi_error) { die_json('Bad request') }
elsif ($action eq 'ergopay') {
    eval { act_ergopay(); 1 } or do { warn "sigmalithos: ergopay failed: $@"; ergopay_error('Something went wrong. Please try again.') };
}
elsif ($action eq 'notify') {
    eval { act_notify(); 1 } or do { warn "sigmalithos: notify failed: $@"; notify_json(undef, 0, 240) };
}
elsif (exists $mosaik{$action}) {
    eval { $mosaik{$action}->(); 1 } or do { warn "sigmalithos: '$action' failed: $@"; mosaik_dialog('Something went wrong. Please try again.') };
}
else { die_json('Unknown action') }
exit;

# ================================================================= helpers ==

sub posted_values {
    my $raw = $cgi->param('POSTDATA');
    return {} unless defined $raw && length $raw;
    my $v = eval { JSON::PP->new->utf8->decode($raw) };
    return ref $v eq 'HASH' ? $v : {};
}
sub good_address { my ($a) = @_; return (defined $a && !ref $a && $a =~ /^[1-9A-HJ-NP-Za-km-z]{40,60}\z/) ? $a : undef }
sub ergopay_host { my $h = $BASE; $h =~ s{^https?://}{}; return $h }
sub erg  { return fmt_amount($_[0], 9, $_[1]) . ' ERG' }
sub lit  { return fmt_amount($_[0], 9, $_[1]) . ' LIT' }
sub unit { my ($id, $raw, $max) = @_; return $id eq ERG ? erg($raw, $max) : lit($raw, $max) }
sub commas { my ($n) = @_; 1 while $n =~ s/^(\d+)(\d{3})/$1,$2/; return $n }
sub num  {    # a JSON number for display: 1234.5678 -> "1,234.57"
    my ($v, $dec) = @_;
    return '?' unless defined $v && !ref $v && $v =~ /^-?\d+(\.\d+)?([eE][-+]?\d+)?\z/;
    my $s = sprintf('%.' . ($dec // 2) . 'f', $v);
    my ($i, $f) = split /\./, $s;
    return commas($i) . (defined $f ? ".$f" : '');
}
sub big_unit {
    my ($n) = @_;
    return '?' unless defined $n && $n =~ /^\d+(\.\d+)?([eE][-+]?\d+)?\z/;
    for my $u ([1e15, 'P'], [1e12, 'T'], [1e9, 'G'], [1e6, 'M'], [1e3, 'k']) { return sprintf('%.1f %s', $n / $u->[0], $u->[1]) if $n >= $u->[0] }
    return sprintf('%.0f ', $n);
}
sub ago {
    my ($ts) = @_;
    my $d = time - $ts;
    return 'just now' if $d < 45;
    return int($d / 60 + 0.5) . ' min ago' if $d < 5400;
    return int($d / 3600 + 0.5) . ' h ago' if $d < 2 * 86400;
    return int($d / 86400 + 0.5) . ' days ago';
}
sub signed {    # "+1.2345 ERG" / "-0.5 ERG" from a raw signed amount
    my ($raw, $unit) = @_;
    my $neg = $raw =~ s/^-//;
    return ($neg ? "\x{2212}" : '+') . fmt_amount($raw, 9, 4) . " $unit";
}

sub stats {    # Lithos's public statistics, cached for a minute
    my $cd   = cache_dir();
    my $file = $cd ? File::Spec->catfile($cd, 'lithos-stats.json') : undef;
    my $read = sub {
        open(my $fh, '<', $file) or return undef;
        local $/;
        my $d = eval { JSON::PP->new->utf8->decode(scalar <$fh>) };
        close $fh;
        return (ref $d eq 'HASH' && ref $d->{stats} eq 'HASH') ? $d : undef;
    };
    if ($file && -f $file && time - (stat $file)[9] < 60) { my $d = $read->(); return $d if $d }
    my $r = chain('lithosstats', {}, 45);
    if ($r->{ok} && ref $r->{stats} eq 'HASH') {
        if ($file) {
            my $tmp = "$file.$$";
            if (open(my $fh, '>', $tmp)) { print {$fh} JSON::PP->new->utf8->encode($r); close $fh; rename($tmp, $file) or unlink $tmp }
        }
        return $r;
    }
    return ($file && -f $file) ? $read->() : undef;
}

# ============================================================ Mosaik views ==

sub base_actions {
    return (
        O(type => 'BackendRequestAction', url => "$API?action=home",   id => 'home',    postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=dash",   id => 'goDash',  postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=mine",   id => 'goMine',  postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=trade",  id => 'goTrade', postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=quote",  id => 'preview', postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=orders", id => 'orders',  postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=liq",    id => 'goLiq',   postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=lend",   id => 'goLend',  postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=lendq",  id => 'lendPreview', postValues => 'VALID'),
        O(type => 'ChangeSiteAction', id => 'clearLend',
          newContent => O(view => O(type => 'Box', id => 'lendresult', children => [ button('Preview', 'lendPreview') ]))),
        O(type => 'BackendRequestAction', url => "$API?action=liqq",   id => 'liqPreview', postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=liqpos", id => 'liqPos',  postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=orders&k=liq", id => 'liqOrders', postValues => 'VALID'),
        O(type => 'ChangeSiteAction', id => 'clearLiq',
          newContent => O(view => O(type => 'Box', id => 'liqresult', children => [ button('Preview', 'liqPreview') ]))),
        O(type => 'BrowserAction', url => 'https://lithosprotocol.org/', id => 'openLithos'),
        O(type => 'ChangeSiteAction', id => 'clearResult',
          newContent => O(view => O(type => 'Box', id => 'result', children => [ button('Preview order', 'preview') ]))),
    );
}
sub mosaik_action { out_json(O(appVersion => N(APP_VERSION), action => $_[0])) }
sub mosaik_dialog { mosaik_action(O(type => 'DialogAction', id => 'msg', message => $_[0], positiveButtonText => 'OK')) }
sub mosaik_replace {
    my ($view, @extra) = @_;
    mosaik_action(O(type => 'ChangeSiteAction', id => 'show', newContent => O(actions => [ base_actions(), @extra ], view => $view)));
}
sub header_rows {
    my ($title, $back) = @_;
    return child(row([ child(label($title, 'HEADLINE2'), weight => N(1)), button('Change', $back || 'home', style => 'TEXT') ]), align => 'JUSTIFY');
}
sub section { my ($title, @rows) = @_; return child(card(column([ child(label($title, 'BODY1BOLD'), align => 'START'), @rows ],
                                                              padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY') }
sub note    { return child(label($_[0], 'BODY2', color => 'SECONDARY'), align => 'START') }
sub address_card {
    my ($addr, $on_change, $text) = @_;
    return child(card(column([
        child(label('Your wallet address', 'BODY1BOLD'), align => 'START'),
        O(type => 'ErgoAddressChooseButton', id => 'address', value => $addr, onValueChanged => $on_change,
          style => $addr ? 'BUTTON_SECONDARY' : undef),
        defined $text ? note($text) : (),
    ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');
}
sub result_box {
    my ($rows) = @_;
    return O(type => 'Box', id => 'result',
             children => [ child(card(column($rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]);
}

sub home_view {
    my ($addr) = @_;
    my $choice = sub {
        my ($title, $what, $more, $btn, $act, $style) = @_;
        return child(card(column([
            child(label($title, 'HEADLINE2'), align => 'START'),
            child(label($what, 'BODY1'), align => 'START'),
            note($more),
            button($btn, $act, style => $style),
        ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');
    };
    return column([
        label('SigmaLithos', 'HEADLINE1', color => 'PRIMARY'),
        label('Lithos, the decentralized mining pool on Ergo, in your wallet', 'BODY2', color => 'SECONDARY', talign => 'CENTER'),
        address_card($addr, 'home', $addr ? undef : 'Choose it once here. "My mining" and "Trade LIT" use it; the dashboard does not need it.'),
        $choice->('Pool dashboard', 'Hashrate, blocks, what mining on Lithos pays compared with ordinary mining, the collateral market and the LIT token.',
                  'Read-only. The figures are the ones Lithos publishes.', 'Open the dashboard', 'goDash'),
        $choice->('My mining', 'What a mining address earned from Lithos lately: payouts in ERG and LIT, blocks found and what the share proofs cost.',
                  'Read-only, from the address\'s own transactions on the blockchain. It does not connect to your miner.', 'Show my mining', 'goMine', 'SECONDARY'),
        ($TOO_OLD
            ? child(card(column([
                  child(label('Trade LIT', 'HEADLINE2'), align => 'START'),
                  child(label('Buy LIT with ERG or sell LIT for ERG on LithosDex, the exchange built into Lithos.', 'BODY1'), align => 'START'),
                  child(label(too_old_text(), 'BODY2BOLD', color => 'PRIMARY'), align => 'START'),
              ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY')
            : $choice->('Trade LIT', 'Buy LIT with ERG or sell LIT for ERG on LithosDex, the exchange built into Lithos.',
                  'You place an order; a Lithos miner fills it at the pool price, never below your minimum. You can cancel it until then.'
                  . (defined $WALLET_OK ? '' : " Needs Ergo wallet app version $MIN_TEXT or newer."), 'Trade LIT', 'goTrade', 'SECONDARY')),
        ($TOO_OLD
            ? child(card(column([
                  child(label('Provide liquidity', 'HEADLINE2'), align => 'START'),
                  child(label(too_old_text(), 'BODY2BOLD', color => 'PRIMARY'), align => 'START'),
              ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY')
            : $choice->('Provide liquidity', 'Add ERG and LIT to the LithosDex pool and earn a share of every trade\'s fees. See your positions, collect fees, close a position.',
                  'Your share moves with the price, so you can end up with less than if you had simply held. A position is owned by an NFT in your wallet: '
                  . 'whoever holds that NFT owns the position.' . (defined $WALLET_OK ? '' : " Needs Ergo wallet app version $MIN_TEXT or newer."),
                  'Provide liquidity', 'goLiq', 'SECONDARY')),
        ($TOO_OLD
            ? child(card(column([
                  child(label('Lend collateral', 'HEADLINE2'), align => 'START'),
                  child(label(too_old_text(), 'BODY2BOLD', color => 'PRIMARY'), align => 'START'),
              ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY')
            : $choice->('Lend collateral', 'Lend ERG to the pool so miners are paid the moment a block is found, and earn the difference when that block\'s own reward reaches you.',
                  'Locks about 3 ERG and a few thousand LIT until a miner uses them, and cannot be cancelled once placed. One position per address.'
                  . (defined $WALLET_OK ? '' : " Needs Ergo wallet app version $MIN_TEXT or newer."),
                  'Lend collateral', 'goLend', 'SECONDARY')),
        label('An independent app, not made by the Lithos project. It cannot set up or control a miner: that needs the Lithos client on your own computer.',
              'BODY2', color => 'SECONDARY', talign => 'CENTER'),
        button('lithosprotocol.org', 'openLithos', style => 'TEXT'),
        label('Your wallet app reports version ' . wallet_text() . '.', 'BODY2', color => 'SECONDARY', talign => 'CENTER'),
    ], id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

sub dash_view {
    my ($addr) = @_;
    # no address field on this screen, so the way back carries the address in its own action (see act_dash)
    my @kids = (header_rows('Pool dashboard', 'homeKeep'));
    my $d = stats();
    if (!$d) {
        push @kids, label('Lithos is not answering right now. Please try again in a moment.', 'BODY1', talign => 'CENTER'), button('Retry', 'dashKeep');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    my $s  = $d->{stats};
    my $l  = ref $s->{lithos} eq 'HASH' ? $s->{lithos} : {};
    my $m  = ref $s->{mining} eq 'HASH' ? $s->{mining} : {};
    my $pr = ref $m->{profitability} eq 'HASH' ? $m->{profitability} : {};
    my $mi = ref $m->{miners} eq 'HASH' ? $m->{miners} : {};
    my $cm = ref $s->{collateralMarket} eq 'HASH' ? $s->{collateralMarket} : {};
    my $dx = ref $s->{dex} eq 'HASH' ? $s->{dex} : {};
    my $su = ref $d->{supply} eq 'HASH' ? $d->{supply} : {};
    my $row = sub { my ($k, $v, %o) = @_; return defined $v ? kv_row($k, $v, %o) : () };
    my $have = sub { my ($h, @path) = @_; for (@path) { return undef unless ref $h eq 'HASH'; $h = $h->{$_} } return (defined $h && !ref $h) ? $h : undef };

    my @pool;
    push @pool, kv_row('Hashrate', big_unit($l->{hashrate}) . 'H/s', style => 'HEADLINE2', color => 'PRIMARY') if defined $l->{hashrate};
    push @pool, kv_row('Share of the Ergo network', num($l->{networkShare} * 100, 1) . ' %') if defined $l->{networkShare};
    push @pool, kv_row('Blocks, 24 h / 7 days', num($l->{blocks24h}, 0) . ' / ' . num($l->{blocks7d}, 0)) if defined $l->{blocks24h} && defined $l->{blocks7d};
    push @pool, kv_row('Last block', ago(int($have->($l, 'lastBlock', 'timestamp') / 1000))) if $have->($l, 'lastBlock', 'timestamp');
    push @pool, kv_row('Miners registered', num($mi->{registered}, 0)) if defined $mi->{registered};
    push @pool, kv_row('Miners paid per block', num($mi->{activePerRollup}, 1)) if defined $mi->{activePerRollup};
    push @pool, kv_row('Paid out so far', num($have->($l, 'payouts', 'erg'), 0) . ' ERG + ' . num($have->($l, 'payouts', 'lit'), 0) . ' LIT')
        if defined $have->($l, 'payouts', 'erg');

    my @pay;
    push @pay, kv_row('Lithos vs ordinary mining', num($pr->{ratio}, 2) . "\x{00D7}", style => 'HEADLINE2', color => 'PRIMARY') if defined $pr->{ratio};
    push @pay, kv_row('A Lithos block pays', "\x{2248} " . num($pr->{rewardErg}, 2) . ' ERG') if defined $pr->{rewardErg};
    push @pay, kv_row('made of', num($pr->{ergPerBlock}, 2) . ' ERG + ' . num($pr->{litPerBlock}, 0) . ' LIT', style => 'BODY1')
        if defined $pr->{ergPerBlock} && defined $pr->{litPerBlock};
    push @pay, kv_row('An ordinary block pays', num($pr->{regularRewardErg}, 2) . ' ERG') if defined $pr->{regularRewardErg};
    push @pay, kv_row('Per MH/s per day', num($pr->{ergPerMhDay}, 5) . ' ERG') if defined $pr->{ergPerMhDay};
    push @pay, note('Lithos\'s own estimate from recent blocks, with LIT counted at its LithosDex price. LIT is thinly traded: '
                    . 'selling a lot of it would fetch less.') if @pay;

    my @litr;
    push @litr, kv_row('LIT price', num($su->{priceErg}, 6) . ' ERG', style => 'HEADLINE2', color => 'PRIMARY') if defined $su->{priceErg};
    push @litr, kv_row('in US dollars', '$' . num($su->{priceUsd}, 6)) if defined $su->{priceUsd};
    push @litr, kv_row('Change, 24 h', ($su->{change24h} >= 0 ? '+' : "\x{2212}") . num(abs($su->{change24h}) * 100, 1) . ' %') if defined $su->{change24h};
    push @litr, kv_row('In circulation', num($have->($su, 'supply', 'circulating') / 1e6, 1) . ' M of 1,000 M') if $have->($su, 'supply', 'circulating');
    push @litr, kv_row('LithosDex volume, 24 h', num($dx->{volume24hErg}, 0) . ' ERG') if defined $dx->{volume24hErg};
    push @litr, kv_row('LithosDex liquidity', num($dx->{tvlErg}, 0) . ' ERG') if defined $dx->{tvlErg};

    my @col;
    my ($act, $q, $le, $bid) = map { ref $cm->{$_} eq 'HASH' ? $cm->{$_} : {} } qw(active queue lenders bids);
    push @col, kv_row('Active collateral boxes', num($act->{boxes}, 0) . ' of ' . num($act->{slots}, 0)) if defined $act->{boxes} && defined $act->{slots};
    push @col, kv_row('Waiting in the queue', num($q->{boxes}, 0)) if defined $q->{boxes};
    push @col, kv_row('LIT permit to join now', num($cm->{nextPermitLit}, 0) . ' LIT') if defined $cm->{nextPermitLit};
    push @col, kv_row('Best bid', num($bid->{bestErg}, 3) . ' ERG') if defined $bid->{bestErg};
    push @col, kv_row('Lender return per cycle', num($le->{roiPercent}, 2) . ' % in about ' . num($le->{cycleDays}, 0) . ' days')
        if defined $le->{roiPercent} && defined $le->{cycleDays};
    push @col, note('Lending collateral is done in the Lithos client, not here: a mistake there can forfeit the deposit.') if @col;

    my @blocks;
    for my $b (@{ ref $d->{blocks} eq 'ARRAY' ? $d->{blocks} : [] }) {
        next unless ref $b eq 'HASH' && ($b->{height} // '') =~ /^\d+\z/ && ($b->{timestamp} // '') =~ /^\d+\z/;
        push @blocks, kv_row(commas($b->{height}) . '  ' . ago(int($b->{timestamp} / 1000)),
                             num($b->{ergToMiners}, 2) . ' ERG + ' . num($b->{litToMiners}, 0) . ' LIT', style => 'BODY1');
        last if @blocks >= 6;
    }

    push @kids, section('Lithos pool', @pool) if @pool;
    push @kids, section('What mining pays', @pay) if @pay;
    push @kids, section('LIT token', @litr) if @litr;
    push @kids, section('Collateral market', @col) if @col;
    push @kids, section('Latest Lithos blocks', @blocks, note('What each block set aside for its miners.')) if @blocks;
    push @kids, button('Refresh', 'dashKeep', style => 'TEXT');
    push @kids, label('Figures published by Lithos (lithosprotocol.org).', 'BODY2', color => 'SECONDARY', talign => 'CENTER');
    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

sub mine_view {
    my ($addr) = @_;
    my @kids = (header_rows('My mining'),
                address_card($addr, 'goMine', 'The address your Lithos client mines to. Only looked up, never stored.'));
    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT') unless $addr;
    my $r = chain('lithosminer', { address => $addr }, 90);
    if (!$r->{ok}) {
        push @kids, label($r->{error} || 'Could not load this address. Please try again.', 'BODY1', talign => 'CENTER'), button('Retry', 'goMine');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    my @ev = grep { ref $_ eq 'HASH' && ($_->{erg} // '') =~ /^-?\d+\z/ && ($_->{lit} // '') =~ /^-?\d+\z/ && ($_->{kind} // '') =~ /^(payout|found|proof)\z/ }
             @{ ref $r->{events} eq 'ARRAY' ? $r->{events} : [] };
    if (!@ev) {
        push @kids, section('Nothing from Lithos here',
            note("None of this address's last " . ($r->{scanned} || 0) . ' transactions is a Lithos payout, a block found or a share proof. '
                 . 'Use the address your Lithos client pays to (the first address of its node wallet).'));
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    my (%erg, %lit, %n);
    for my $e (@ev) {
        $erg{ $e->{kind} } = ($erg{ $e->{kind} } || Math::BigInt->new(0))->badd($e->{erg});
        $lit{ $e->{kind} } = ($lit{ $e->{kind} } || Math::BigInt->new(0))->badd($e->{lit});
        $n{ $e->{kind} }++;
    }
    my $net_erg = Math::BigInt->new(0); $net_erg->badd($_) for values %erg;
    my $net_lit = Math::BigInt->new(0); $net_lit->badd($_) for values %lit;
    my $since   = ($r->{since} // 0) =~ /^\d+\z/ ? $r->{since} : 0;
    my $days    = $since ? (time - $since) / 86400 : 0;
    my $period  = !$since ? 'recent transactions' : $days < 2 ? 'the last ' . int($days * 24 + 0.5) . ' hours' : 'the last ' . int($days + 0.5) . ' days';
    my $partial = ($r->{total} || 0) > ($r->{scanned} || 0);

    my @sum = (
        kv_row('Net ERG', signed($net_erg->bstr, 'ERG'), style => 'HEADLINE2', color => 'PRIMARY'),
        kv_row('Net LIT', signed($net_lit->bstr, 'LIT'), style => 'HEADLINE2', color => 'PRIMARY'),
    );
    push @sum, kv_row('Per day, about', signed(int($net_erg->numify / $days), 'ERG') . ' and ' . signed(int($net_lit->numify / $days), 'LIT'), style => 'BODY1')
        if $days >= 0.5;
    push @sum, note("Over $period" . ($partial ? ", which is as far back as this address's last $r->{scanned} transactions reach" : '')
                    . '. Payouts minus what the share proofs cost; a bond posted with a proof comes back in that block\'s payout, so recent '
                    . 'bonds still waiting make the ERG figure look lower than it will be.');
    my @parts;
    push @parts, kv_row("Payouts ($n{payout})", signed($erg{payout}->bstr, 'ERG') . '  ' . signed($lit{payout}->bstr, 'LIT'), style => 'BODY1') if $n{payout};
    push @parts, kv_row("Blocks found ($n{found})", signed($erg{found}->bstr, 'ERG') . '  ' . signed($lit{found}->bstr, 'LIT'), style => 'BODY1') if $n{found};
    push @parts, kv_row("Share proofs ($n{proof})", signed($erg{proof}->bstr, 'ERG'), style => 'BODY1') if $n{proof};
    my @recent;
    for my $e (grep { $_->{kind} ne 'proof' } @ev) {
        push @recent, kv_row(($e->{kind} eq 'found' ? 'Found a block, ' : 'Payout, ') . ago($e->{ts} || 0),
                             signed($e->{erg}, 'ERG') . '  ' . signed($e->{lit}, 'LIT'), style => 'BODY1');
        last if @recent >= 8;
    }
    push @kids, section('Earned from Lithos', @sum), section('Made up of', @parts);
    push @kids, section('Latest', @recent) if @recent;
    push @kids, button('Refresh', 'goMine', style => 'TEXT');
    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

sub trade_state {
    my ($v) = @_;
    my %s = (address => good_address($v->{address}));
    $s{side} = (defined $v->{side} && !ref $v->{side} && $v->{side} eq 'sell') ? 'sell' : 'buy';
    my $a = $v->{amount_9};
    $s{amount} = "$a" if defined $a && !ref $a && "$a" =~ /^[1-9]\d{0,18}\z/;
    my %slip = SLIPPAGE;
    my $sl   = $v->{slippage};
    $s{slippage} = (defined $sl && !ref $sl && exists $slip{$sl}) ? $sl : 100;
    return \%s;
}

sub trade_view {
    my ($s, $result) = @_;
    my $q    = chain('ldquote', { $s->{address} ? (address => $s->{address}) : () });
    my $buy  = $s->{side} eq 'buy';
    my %slip = SLIPPAGE;
    my @kids = (header_rows('Trade LIT'), address_card($s->{address}, 'goTrade'));
    if (!$q->{ok}) {
        push @kids, label($q->{error} || 'LithosDex is unavailable right now.', 'BODY1', talign => 'CENTER'), button('Retry', 'goTrade');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    my @form = (
        child(label('1 LIT = ' . fmt_amount($q->{price_9}, 9) . ' ERG on LithosDex', 'BODY1', color => 'PRIMARY'), align => 'START'),
        child(O(type => 'DropDownList', id => 'side', value => $s->{side}, onValueChanged => 'goTrade',
                entries => O(buy => 'Buy LIT with ERG', sell => 'Sell LIT for ERG')), align => 'JUSTIFY'),
        child(label($buy ? 'ERG to spend' : 'LIT to sell', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DecimalInputField', id => 'amount_9', value => defined $s->{amount} ? N($s->{amount}) : undef,
                placeholder => $buy ? 'Amount of ERG' : 'Amount of LIT', onValueChanged => 'clearResult', onImeAction => 'preview',
                imeActionType => 'DONE', minValue => N(1), scale => N(9)), align => 'JUSTIFY'),
    );
    push @form, child(label('You hold: ' . erg($q->{balance_erg}, 4) . ' and ' . lit($q->{balance_lit}, 4), 'BODY2', color => 'SECONDARY'), align => 'END')
        if defined $q->{balance_erg} && defined $q->{balance_lit};
    push @form,
        child(label('Slippage tolerance (sets your minimum)', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DropDownList', id => 'slippage', value => "$s->{slippage}", onValueChanged => 'clearResult',
                entries => O(map { $_ => $slip{$_} } sort { $a <=> $b } keys %slip)), align => 'JUSTIFY');
    push @kids, child(card(column(\@form, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');
    push @kids, child($result || O(type => 'Box', id => 'result', children => [ button('Preview order', 'preview') ]), align => 'JUSTIFY');
    push @kids, child(O(type => 'Box', id => 'orders', children => [ $s->{address} ? button('My open orders', 'orders', style => 'TEXT') : () ]), align => 'JUSTIFY');
    push @kids, label('Orders are filled by Lithos miners, usually within a few minutes. An order whose price is out of reach waits '
                      . 'until the price comes back, or until you cancel it.', 'BODY2', color => 'SECONDARY', talign => 'CENTER');
    # LithosDex contracts use Ergo 6.0 operations. A wallet build without 6.0 support cannot read the
    # order box and stops with "Cannot create ErgoAddress from unparsed ergo tree" before signing.
    push @kids, label("Needs Ergo wallet app version $MIN_TEXT or newer. If signing stops with an error about an \"unparsed ergo tree\", "
                      . 'your wallet is older and cannot read the contract format LithosDex uses: nothing was sent. Update the wallet and try again.',
                      'BODY2', color => 'SECONDARY', talign => 'CENTER') unless $WALLET_OK;
    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

# ================================================================= actions ==

sub act_app {
    out_json(O(
        manifest => O(
            appName => 'SigmaLithos', appDescription => 'The Lithos mining pool: dashboard, your payouts, and LIT trading.',
            iconUrl => "$BASE/icon.png", appVersion => N(APP_VERSION), targetMosaikVersion => N(2),
            targetCanvasDimension => 'COMPACT_WIDTH', cacheLifetime => N(0),
            notificationCheckUrl => "$API?action=notify",
        ),
        actions => [ base_actions() ],
        view    => home_view(undef),
    ));
}
sub act_home  { mosaik_replace(home_view(good_address(posted_values()->{address}) || good_address($cgi->url_param('addr')))) }
sub act_dash  {
    my $addr = good_address(posted_values()->{address}) || good_address($cgi->url_param('addr'));
    my $q    = $addr ? "&addr=$addr" : '';
    mosaik_replace(dash_view($addr),
                   O(type => 'BackendRequestAction', url => "$API?action=home$q", id => 'homeKeep', postValues => 'NONE'),
                   O(type => 'BackendRequestAction', url => "$API?action=dash$q", id => 'dashKeep', postValues => 'NONE'));
}
sub act_mine  { mosaik_replace(mine_view(good_address(posted_values()->{address}))) }
sub act_trade { return mosaik_dialog(too_old_text()) if $TOO_OLD; mosaik_replace(trade_view(trade_state(posted_values()))) }

sub act_quote {
    return mosaik_dialog(too_old_text()) if $TOO_OLD;
    my $s = trade_state(posted_values());
    return mosaik_dialog('Enter the amount first.') unless defined $s->{amount};
    my $buy = $s->{side} eq 'buy';
    my $q = chain('ldquote', { side => $s->{side}, amount => $s->{amount}, ($s->{address} ? (address => $s->{address}) : ()) });
    return mosaik_dialog($q->{error} || 'Could not get a price. Please try again.') unless $q->{ok};
    return mosaik_dialog('Could not get a price. Please try again.') unless ($q->{out} // '') =~ /^[1-9]\d*\z/;

    my $min = Math::BigInt->new($q->{out})->bmul(10000 - $s->{slippage})->bdiv(10000);
    $min = Math::BigInt->new(1) if $min->is_zero;
    my $fee = cfg('MINER_FEE', 1100000) + 0;
    my $bps = $q->{impact_bps} + 0;
    my ($from, $to) = $buy ? (ERG, LIT) : (LIT, ERG);
    my @rows = (
        kv_row('You pay', unit($from, $s->{amount})),
        kv_row('You receive (estimate)', "\x{2248} " . unit($to, $q->{out}, 4), style => 'HEADLINE2', color => 'PRIMARY'),
        kv_row('At least', unit($to, $min->bstr, 4)),
        O(type => 'HorizontalRule', vPadding => 'HALF_DEFAULT'),
        kv_row('Price impact', sprintf('%.2f %%', $bps / 100)),
        kv_row('Pool fees', sprintf('%.2f %%', $q->{pool_fee_bps} / 100)),
        kv_row('Fee to the miner who fills it', erg($q->{executor_fee}) . ($buy ? ' on top' : ', from the ERG you receive')),
        kv_row('Network fee', erg($fee)),
        note('This places an order. A Lithos miner fills it in a block it mines, at the pool price at that moment and never below your '
             . 'minimum. That usually takes a few minutes, sometimes longer. Other trades can move the price before then, in either '
             . 'direction. Until it is filled you can cancel it under "My open orders" and get everything back except the network fees; '
             . 'an order that fills within minutes leaves little time for that. '
             . erg($q->{returned}) . ' rides along in the order box and comes back to you with the fill.'),
    );
    push @rows, child(label('Warning: this trade moves the pool price by ' . sprintf('%.1f %%', $bps / 100) . '. The LIT pool is small; '
                            . 'a smaller amount gets a better price.', 'BODY2BOLD', color => 'PRIMARY'), align => 'START') if $bps >= 300;
    my @extra;
    my $have = $buy ? $q->{balance_erg} : $q->{balance_lit};
    my $need = Math::BigInt->new($s->{amount});
    $need->badd($q->{executor_fee} + $q->{returned} + $fee + 1000000) if $buy;
    my $short_erg = !$buy && defined $q->{balance_erg}
                    && Math::BigInt->new($q->{balance_erg})->bcmp($q->{returned} + $fee + 1000000) < 0;
    if (!$s->{address}) {
        push @rows, label('Choose your wallet address above to place the order.', 'BODY1', talign => 'CENTER');
    }
    elsif (defined $have && $need->bcmp(Math::BigInt->new($have)) > 0) {
        push @rows, label('Not enough ' . ($buy ? 'ERG: the order also needs the fees.' : 'LIT on this address.'), 'BODY1', talign => 'CENTER');
    }
    elsif ($short_erg) {
        push @rows, label('Not enough ERG: selling LIT still needs about 0.004 ERG for the order box and the network fee.', 'BODY1', talign => 'CENTER');
    }
    else {
        my $url = 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&side=$s->{side}&a=$s->{amount}&m=" . $min->bstr . "&addr=$s->{address}";
        my $nonce = new_nonce();
        my $ok = defined $nonce && record_file($nonce);
        if ($ok) {
            $url .= "&n=$nonce";
            watch_add('lithos', $nonce);
            push @extra, O(type => 'BackendRequestAction', url => "$API?action=status&n=$nonce", id => 'status', postValues => 'NONE');
        }
        push @extra, O(type => 'ErgoPayAction', url => $url, id => 'pay', onFinished => $ok ? 'status' : 'goTrade');
        push @rows, child(row([
            O(type => 'Icon', icon => 'WARN', size => 'SMALL', tintColor => 'PRIMARY'),
            child(label("Your Ergo wallet app must be version $MIN_TEXT or newer to sign this. Older versions stop with an error.",
                        'BODY2BOLD', color => 'PRIMARY'), weight => N(1)),
        ], spacing => 'HALF_DEFAULT'), align => 'JUSTIFY') unless $WALLET_OK;
        push @rows, button($buy ? 'Place order: buy LIT' : 'Place order: sell LIT', 'pay');
    }
    push @rows, button('Refresh', 'preview', style => 'TEXT');
    mosaik_replace(result_box(\@rows), @extra);
}

sub act_orders {
    my $addr = good_address(posted_values()->{address}) or return mosaik_dialog('Choose your wallet address first.');
    my $liq  = (($cgi->url_param('k') // '') eq 'liq') ? 1 : 0;
    my $r = chain('ldorders', { address => $addr, kinds => $liq ? 'liquidity' : 'swap' }, 60);
    return mosaik_dialog($r->{error} || 'Could not load your orders. Please try again.') unless $r->{ok};
    my $refresh = $liq ? 'liqOrders' : 'orders';
    my (@rows, @extra);
    push @rows, child(label('Open orders', 'BODY1BOLD'), align => 'START');
    my $n = 0;
    for my $o (@{ ref $r->{orders} eq 'ARRAY' ? $r->{orders} : [] }) {
        next unless ref $o eq 'HASH' && ($o->{box_id} // '') =~ /^[0-9a-f]{64}\z/ && ($o->{kind} // '') =~ /^(sell|buy|deposit|redeem)\z/;
        my $what;
        if ($o->{kind} eq 'deposit') {
            next unless ($o->{amount_x} // '') =~ /^\d+\z/ && ($o->{amount_y} // '') =~ /^\d+\z/;
            $what = 'Add ' . erg($o->{amount_x}, 4) . ' and ' . lit($o->{amount_y}, 4) . ' to the pool';
        }
        elsif ($o->{kind} eq 'redeem') {
            next unless ($o->{nft} // '') =~ /^[0-9a-f]{64}\z/;
            $what = 'Close position ' . substr($o->{nft}, 0, 6) . "\x{2026}";
        }
        else {
            next unless ($o->{amount} // '') =~ /^\d+\z/ && ($o->{min_out} // '') =~ /^\d+\z/
                     && ($o->{from} // '') =~ /^[0-9a-f]{64}\z/ && ($o->{to} // '') =~ /^[0-9a-f]{64}\z/;
            $what = 'Sell ' . unit($o->{from}, $o->{amount}) . ' for at least ' . unit($o->{to}, $o->{min_out}, 4);
        }
        $n++;
        push @extra, O(type => 'ErgoPayAction', id => "cancel$n", onFinished => $refresh,
                       url => 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&r=$o->{box_id}&addr=$addr");
        push @rows,
            O(type => 'HorizontalRule', vPadding => 'QUARTER_DEFAULT'),
            child(label($what, 'BODY1'), align => 'START'),
            button('Cancel and take it back', "cancel$n", style => 'SECONDARY');
        last if $n >= 20;
    }
    push @rows, note($liq ? 'No open liquidity orders on this address. An order that was filled is no longer listed: see "My positions".'
                          : 'No open LIT orders on this address. An order that was filled is no longer listed: what it bought is in your wallet, '
                            . 'and "You hold" above shows it after a refresh. A fill can happen within minutes when a Lithos miner finds the next block.') unless $n;
    push @rows, note('An order still here is waiting for a Lithos miner, or for the price to come back within its limit. If a cancel is '
                     . 'refused as a "double spending attempt", a miner was filling the order at that moment: refresh, and it will be gone.') if $n;
    push @rows, button('Refresh', $refresh, style => 'TEXT');
    mosaik_replace(O(type => 'Box', id => $liq ? 'liqorders' : 'orders',
                     children => [ child(card(column(\@rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]), @extra);
}

# ============================================================== liquidity ==

sub liq_box {
    my ($id, $rows) = @_;
    return O(type => 'Box', id => $id,
             children => [ child(card(column($rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]);
}

sub liq_view {
    my ($v) = @_;
    my $addr = good_address($v->{address});
    my @kids = (header_rows('Provide liquidity'), address_card($addr, 'goLiq'));
    my $q = chain('ldlpquote', { $addr ? (address => $addr) : () });
    if (!$q->{ok}) {
        push @kids, label($q->{error} || 'LithosDex is unavailable right now.', 'BODY1', talign => 'CENTER'), button('Retry', 'goLiq');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    my $a = $v->{lx_9};
    $a = (defined $a && !ref $a && "$a" =~ /^[1-9]\d{0,18}\z/) ? "$a" : undef;
    my @form = (
        child(label('The pool holds ' . erg($q->{reserve_erg}, 0) . ' and ' . lit($q->{reserve_lit}, 0), 'BODY1', color => 'PRIMARY'), align => 'START'),
        child(label('ERG to add', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DecimalInputField', id => 'lx_9', value => defined $a ? N($a) : undef, placeholder => 'Amount of ERG',
                onValueChanged => 'clearLiq', onImeAction => 'liqPreview', imeActionType => 'DONE', minValue => N(1), scale => N(9)), align => 'JUSTIFY'),
        note('The matching amount of LIT is added with it, at the pool\'s current price. Preview shows how much.'),
    );
    push @form, child(label('You hold: ' . erg($q->{balance_erg}, 4) . ' and ' . lit($q->{balance_lit}, 4), 'BODY2', color => 'SECONDARY'), align => 'END')
        if defined $q->{balance_erg} && defined $q->{balance_lit};
    push @kids, child(card(column([ child(label('Add liquidity', 'BODY1BOLD'), align => 'START'), @form ],
                                  padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');
    push @kids, child(O(type => 'Box', id => 'liqresult', children => [ button('Preview', 'liqPreview') ]), align => 'JUSTIFY');
    push @kids, child(O(type => 'Box', id => 'liqpos', children => [ $addr ? button('My positions', 'liqPos', style => 'SECONDARY') : () ]), align => 'JUSTIFY');
    push @kids, child(O(type => 'Box', id => 'liqorders', children => [ $addr ? button('My open liquidity orders', 'liqOrders', style => 'TEXT') : () ]), align => 'JUSTIFY');
    push @kids, label('Providers earn ' . sprintf('%.2f %%', $q->{pool_fee_bps} / 100) . ' of every trade, shared by size. When the price moves the pool '
                      . 'rebalances, so you get back a different mix of ERG and LIT than you put in, which can be worth less than holding.',
                      'BODY2', color => 'SECONDARY', talign => 'CENTER');
    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

# ================================================================ lending ==

use constant BIDS => (0 => 'No fee: most profit, picked last', 10000000 => 'Small fee', 30000000 => 'Medium fee', 50000000 => 'Typical fee: picked sooner, least profit');

sub lend_view {
    my ($v) = @_;
    my $addr = good_address($v->{address});
    my @kids = (header_rows('Lend collateral'), address_card($addr, 'goLend'));
    my $i = chain('lendinfo', { $addr ? (address => $addr) : () }, 90);
    if (!$i->{ok}) {
        push @kids, label($i->{error} || 'Lithos lending is unavailable right now.', 'BODY1', talign => 'CENTER'), button('Retry', 'goLend');
        return (column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT'));
    }
    my $d  = stats();
    my $cm = ($d && ref $d->{stats}{collateralMarket} eq 'HASH') ? $d->{stats}{collateralMarket} : {};
    my $le = ref $cm->{lenders} eq 'HASH' ? $cm->{lenders} : {};
    my @mkt = (
        kv_row('ERG a position locks', erg($i->{principal}) . ' plus your fee'),
        kv_row('LIT permit to join now', lit($i->{permit}, 0), style => 'HEADLINE2', color => 'PRIMARY'),
        kv_row('Positions waiting in the queue', commas($i->{backlog})),
        kv_row('Positions in use', "$i->{active} of 100"),
    );
    push @mkt, kv_row('A full cycle takes about', num($le->{cycleDays}, 0) . ' days') if defined $le->{cycleDays};
    push @mkt, kv_row('Recent return per cycle', num($le->{roiPercent}, 2) . ' %') if defined $le->{roiPercent};
    push @mkt, note('How it works: your position waits its turn in the queue, then joins the 100 in use. When a Lithos miner finds a block with '
                    . 'it, your ERG pays the pool at once, the LIT permit comes straight back to you, and that block\'s own reward (3 ERG plus its '
                    . 'transaction fees) is paid to your key. You can collect it here about a day later. The permit grows with the queue.');
    push @kids, section('The lending market', @mkt);

    my @extra;
    if ($addr) {
        my @you;
        my $state = $i->{state} // 'none';
        if ($state eq 'queued') {
            my $q = (ref $i->{queued} eq 'ARRAY' && ref $i->{queued}[0] eq 'HASH') ? $i->{queued}[0] : {};
            push @you, kv_row('Your position', 'waiting in the queue', style => 'BODY1BOLD', color => 'PRIMARY'),
                       kv_row('Positions ahead of yours', commas($q->{ahead} // 0)),
                       note('It moves up as blocks are found. Nothing to do until then, and it cannot be cancelled.');
        }
        elsif ($state eq 'active') {
            push @you, kv_row('Your position', 'in use', style => 'BODY1BOLD', color => 'PRIMARY'),
                       note('It is among the 100 that miners draw on. When one finds a block with it, the permit returns and the reward is yours a day later.');
        }
        else { push @you, kv_row('Your position', 'none on this address') }
        push @you, kv_row('Rewards ready to collect', erg($i->{rewards_ready}, 4) . " ($i->{rewards_ready_count})") if $i->{rewards_ready_count};
        push @you, kv_row('Rewards still maturing', erg($i->{rewards_waiting}, 4) . " ($i->{rewards_waiting_count})") if $i->{rewards_waiting_count};
        if ($i->{rewards_ready_count}) {
            push @extra, sign_pair('reward', 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&k=reward&addr=$addr", 'goLend');
            push @you, O(type => 'Box', id => 'lendreward', children => [ button('Collect rewards', 'reward', style => 'SECONDARY') ]);
        }
        push @kids, section('You', @you);

        if ($state eq 'none') {
            my %bids = BIDS;
            my $bid  = (defined $v->{bid} && !ref $v->{bid} && exists $bids{ $v->{bid} }) ? $v->{bid} : 50000000;
            push @kids, child(card(column([
                child(label('Open a position', 'BODY1BOLD'), align => 'START'),
                child(label('Priority fee', 'BODY1'), align => 'START'),
                child(O(type => 'DropDownList', id => 'bid', value => "$bid", onValueChanged => 'clearLend',
                        entries => O(map { $_ => $bids{$_} . ' (' . fmt_amount($_, 9) . ' ERG)' } sort { $a <=> $b } keys %bids)), align => 'JUSTIFY'),
                note('A position is 2.915 ERG plus this fee. The fee is given up: a fifth goes to the miner who uses your position, the rest to the pool, so miners pick paying positions first. Profit per use is 0.085 ERG minus the fee, plus the block\'s transaction fees. Most lenders currently pay 0.03 to 0.05 ERG; positions with no fee can wait much longer.'),
                child(label('You hold: ' . erg($i->{balance_erg}, 4) . ' and ' . lit($i->{balance_lit}, 0), 'BODY2', color => 'SECONDARY'), align => 'END'),
            ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');
            push @kids, child(O(type => 'Box', id => 'lendresult', children => [ button('Preview', 'lendPreview') ]), align => 'JUSTIFY');
        }
        else {
            push @kids, label('One position per address: a second one from the same address would be forfeited, so this app does not offer it. '
                              . 'Use another address of your wallet to open another.', 'BODY2', color => 'SECONDARY', talign => 'CENTER');
        }
    }
    push @kids, button('Refresh', 'goLend', style => 'TEXT');
    return (column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT'), @extra);
}

sub act_lend { return mosaik_dialog(too_old_text()) if $TOO_OLD; mosaik_replace(lend_view(posted_values())) }

sub act_lendq {
    return mosaik_dialog(too_old_text()) if $TOO_OLD;
    my $v    = posted_values();
    my $addr = good_address($v->{address}) or return mosaik_dialog('Choose your wallet address first.');
    my %bids = BIDS;
    my $bid  = (defined $v->{bid} && !ref $v->{bid} && exists $bids{ $v->{bid} }) ? $v->{bid} : 50000000;
    my $i = chain('lendinfo', { address => $addr }, 90);
    return mosaik_dialog($i->{error} || 'Lithos lending is unavailable right now.') unless $i->{ok};
    return mosaik_dialog('This address already has a position. One position per address.') unless ($i->{state} // '') eq 'none';
    my $fee    = cfg('MINER_FEE', 1100000) + 0;
    my $value  = $i->{principal} + $bid;
    my $profit = $i->{block_reward} - $value;
    # the permit follows the queue: allow it to have grown by a few joins between preview and signing
    my $max_permit = Math::BigInt->new($i->{permit})->badd('100000000000')->bstr;
    my @rows = (
        kv_row('You lock', erg($value), style => 'HEADLINE2', color => 'PRIMARY'),
        kv_row('and the LIT permit', lit($i->{permit}, 0), style => 'HEADLINE2', color => 'PRIMARY'),
        kv_row('of which priority fee', $bid ? erg($bid) : 'none'),
        kv_row('Positions ahead of yours', commas($i->{backlog})),
        kv_row('Profit when it is used', "\x{2248} " . erg($profit, 4) . ' + block fees'),
        kv_row('Network fee', erg($fee)),
        child(row([
            O(type => 'Icon', icon => 'WARN', size => 'SMALL', tintColor => 'PRIMARY'),
            child(label('This cannot be cancelled. The ERG and the LIT stay locked until a miner uses your position. How long that takes depends on the queue and on how often Lithos finds blocks: days at best, possibly much longer.',
                        'BODY2BOLD', color => 'PRIMARY'), weight => N(1)),
        ], spacing => 'HALF_DEFAULT'), align => 'JUSTIFY'),
        note('When it is used you get the LIT permit back at once, and the block\'s reward about a day later under "Collect rewards". The LIT '
             . 'price can change while it is locked. If Lithos stopped finding blocks, the position would simply keep waiting. Do not open a second '
             . 'position from this address anywhere else while this one is open: Lithos\'s rules forfeit a duplicate.'),
    );
    my @extra;
    if (Math::BigInt->new($i->{balance_lit})->bcmp(Math::BigInt->new($i->{permit})) < 0) {
        push @rows, label('Not enough LIT: the permit is ' . lit($i->{permit}, 0) . ' and this address holds ' . lit($i->{balance_lit}, 0)
                          . '. You can buy LIT under "Trade LIT".', 'BODY1', talign => 'CENTER');
    }
    elsif (Math::BigInt->new($i->{balance_erg})->bcmp($value + $fee + 2000000) < 0) {
        push @rows, label('Not enough ERG: the position needs ' . erg($value) . ' plus the network fee.', 'BODY1', talign => 'CENTER');
    }
    else {
        push @extra, sign_pair('join', 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&k=join&a=$bid&my=$max_permit&addr=$addr", 'goLend');
        push @rows, button('Lend: open the position', 'join');
    }
    push @rows, button('Refresh', 'lendPreview', style => 'TEXT');
    mosaik_replace(liq_box('lendresult', \@rows), @extra);
}

sub act_liq { return mosaik_dialog(too_old_text()) if $TOO_OLD; mosaik_replace(liq_view(posted_values())) }

sub sign_pair {    # (status action, ErgoPayAction) for one signing link, tied together by a fresh note id
    my ($id, $url, $after) = @_;
    my ($kind) = $url =~ /[?&]k=(dep|claim|close|join|reward)\b/;
    my $nonce = new_nonce();
    my $ok = defined $nonce && record_file($nonce);
    my @a;
    if ($ok) {
        $url .= "&n=$nonce";
        watch_add('lithos', $nonce);
        push @a, O(type => 'BackendRequestAction', url => "$API?action=lstatus&n=$nonce&k=" . ($kind // 'dep'), id => "st_$id", postValues => 'NONE');
    }
    push @a, O(type => 'ErgoPayAction', url => $url, id => $id, onFinished => $ok ? "st_$id" : $after);
    return @a;
}

sub act_liqq {
    return mosaik_dialog(too_old_text()) if $TOO_OLD;
    my $v    = posted_values();
    my $addr = good_address($v->{address});
    my $a    = $v->{lx_9};
    return mosaik_dialog('Enter how much ERG you want to add.') unless defined $a && !ref $a && "$a" =~ /^[1-9]\d{0,18}\z/;
    my $q = chain('ldlpquote', { amount => "$a", ($addr ? (address => $addr) : ()) });
    return mosaik_dialog($q->{error} || 'Could not get a quote. Please try again.') unless $q->{ok};
    return mosaik_dialog('Could not get a quote. Please try again.') unless ($q->{amount_y} // '') =~ /^[1-9]\d*\z/ && ($q->{shares} // '') =~ /^[1-9]\d*\z/;
    my $fee     = cfg('MINER_FEE', 1100000) + 0;
    my $max_y   = Math::BigInt->new($q->{amount_y})->bmul(101)->bdiv(100)->badd(1);     # 1 % either way between preview and signing
    my $min_sh  = Math::BigInt->new($q->{shares})->bmul(99)->bdiv(100);
    $min_sh = Math::BigInt->new(1) if $min_sh->is_zero;
    my $locked  = $q->{provision_min} + $q->{executor_fee} + $q->{returned};
    my @rows = (
        kv_row('You add', erg("$a")),
        kv_row('and', "\x{2248} " . lit($q->{amount_y}, 4), style => 'HEADLINE2', color => 'PRIMARY'),
        kv_row('Your share of the pool', sprintf('%.2f %%', $q->{share_bps} / 100)),
        O(type => 'HorizontalRule', vPadding => 'HALF_DEFAULT'),
        kv_row('Position deposit', erg($q->{provision_min}) . ' (returned when you close)'),
        kv_row('Fee to the miner who fills it', erg($q->{executor_fee})),
        kv_row('Network fee', erg($fee)),
        note('This places a deposit order. A Lithos miner fills it, usually within minutes, and you receive an NFT named LD-NFT: it IS your '
             . 'position. Whoever holds it can collect the fees and withdraw the liquidity, so do not send it anywhere or burn it. If the '
             . 'price has moved more than 1 % when the order is filled, it waits; you can cancel it under "My open liquidity orders".'),
    );
    my @extra;
    my $need_erg = Math::BigInt->new("$a")->badd($locked + $fee + 1000000);
    if (!$addr) { push @rows, label('Choose your wallet address above to continue.', 'BODY1', talign => 'CENTER') }
    elsif (defined $q->{balance_erg} && $need_erg->bcmp(Math::BigInt->new($q->{balance_erg})) > 0) {
        push @rows, label('Not enough ERG: besides the amount, the order needs ' . erg($locked + $fee, 4) . ' for the deposit and fees.', 'BODY1', talign => 'CENTER');
    }
    elsif (defined $q->{balance_lit} && $max_y->bcmp(Math::BigInt->new($q->{balance_lit})) > 0) {
        push @rows, label('Not enough LIT: this address holds ' . lit($q->{balance_lit}, 4) . '. Buy LIT under "Trade LIT" first, or add less ERG.', 'BODY1', talign => 'CENTER');
    }
    else {
        push @extra, sign_pair('dep', 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&k=dep&a=$a&my=" . $max_y->bstr . '&ms=' . $min_sh->bstr . "&addr=$addr", 'goLiq');
        push @rows, button('Place deposit order', 'dep');
    }
    push @rows, button('Refresh', 'liqPreview', style => 'TEXT');
    mosaik_replace(liq_box('liqresult', \@rows), @extra);
}

# What happened to a deposit order, a fee collection or a closing order. The answer replaces the area
# the button was in: the preview area for a deposit, the positions area for the other two.
sub liq_state {
    my ($rec) = @_;
    my $kind = $rec->{kind} // '';
    if ($kind =~ /^(claim|join|reward)\z/) {
        my $r = chain('status', { tx_id => $rec->{tx_id}, watch_box => $rec->{watch_box}, mode => 'bridge', address => $rec->{address} });
        return $r->{ok} ? ($r->{state} // 'unknown') : 'unknown';
    }
    my $r = chain('ldstatus', { map { $_ => $rec->{$_} } qw(tx_id watch_box order_box address) });
    return $r->{ok} ? ($r->{state} // 'unknown') : 'unknown';
}

sub act_lstatus {
    my $nonce = $cgi->url_param('n');
    return mosaik_dialog('Unknown request.') unless defined $nonce && $nonce =~ /^[0-9a-f]{32}\z/;
    my $rec  = load_record($nonce);
    my $kind = ($rec && ($rec->{app} // '') eq 'lithos') ? ($rec->{kind} // '') : '';
    # before the wallet fetched anything there is no note yet: the kind is then carried in the link
    $kind = $cgi->url_param('k') // '' unless $kind =~ /^(dep|claim|close|join|reward)\z/;
    $kind = 'dep' unless $kind =~ /^(dep|claim|close|join|reward)\z/;
    my $lend  = $kind =~ /^(join|reward)\z/ ? 1 : 0;
    my $area  = $kind eq 'dep' ? 'liqresult' : $kind eq 'join' ? 'lendresult' : $kind eq 'reward' ? 'lendreward' : 'liqpos';
    my $self  = O(type => 'BackendRequestAction', url => "$API?action=lstatus&n=$nonce&k=$kind", id => 'lstatus', postValues => 'NONE');
    my $again = button('Refresh status', 'lstatus', style => 'SECONDARY');
    my $back  = $lend ? button('Back to lending', 'goLend', style => 'TEXT')
              : $kind eq 'dep' ? button('Back to the form', 'clearLiq', style => 'TEXT') : button('Back to my positions', 'liqPos', style => 'TEXT');
    my $row   = sub { child(label($_[0], $_[1] || 'BODY1'), align => 'START') };
    my @rows;
    if (!$rec || !$rec->{tx_id}) {
        @rows = ($row->('Waiting for your wallet', 'BODY1BOLD'),
                 $row->('Your wallet has not picked this up yet. If you cancelled signing, nothing happened.'), $again, $back);
        return mosaik_replace(liq_box($area, \@rows), $self);
    }
    my $st = liq_state($rec);
    my %done = (dep   => [ 'Liquidity added', 'Your deposit was filled. The LD-NFT in your wallet is your position: see "My positions".' ],
                close => [ 'Position closed', 'Your share of the pool and the position deposit were paid to your address.' ],
                claim => [ 'Fees collected', 'You received ' . erg($rec->{claim_erg} // 0, 6) . ' and ' . lit($rec->{claim_lit} // 0, 4) . '.' ],
                join  => [ 'Position opened', 'You are in the Lithos collateral queue. "Lend collateral" shows how many positions are ahead of yours.' ],
                reward => [ 'Rewards collected', 'You received ' . erg($rec->{net} // 0, 4) . '.' ]);
    if ($st eq 'filled' || ($kind =~ /^(claim|join|reward)\z/ && $st eq 'confirmed')) { @rows = ($row->($done{$kind}[0], 'BODY1BOLD'), $row->($done{$kind}[1]), $back) }
    elsif ($st eq 'open') {
        @rows = ($row->('Waiting for a Lithos miner', 'BODY1BOLD'),
                 $row->('The order is on the blockchain. A Lithos miner usually fills it within a few minutes; you can cancel it under '
                        . '"My open liquidity orders" until then.'), $again, $back);
    }
    elsif ($st eq 'cancelled') { @rows = ($row->('Order cancelled', 'BODY1BOLD'), $row->('Everything in the order is back on your address.'), $back) }
    elsif ($st eq 'pending')   { @rows = ($row->('Submitted', 'BODY1BOLD'), $row->('Waiting for the next Ergo block (about two minutes).'), $again) }
    elsif ($st eq 'failed')    { @rows = ($row->('It did not go through', 'BODY1BOLD'),
                                          $row->('Something it needed was spent by another transaction first. Nothing was lost. Please try again.'), $back) }
    elsif (time - ($rec->{t} || time) < 180) {
        @rows = ($row->('Waiting for the network', 'BODY1BOLD'), $row->('Your transaction has not shown up yet. This can take a moment after signing.'), $again, $back);
    }
    else {
        @rows = ($row->('Not found on the network', 'BODY1BOLD'),
                 $row->("If you cancelled signing, nothing happened. Otherwise check your wallet's history before trying again."), $again, $back);
    }
    mosaik_replace(liq_box($area, \@rows), $self);
}

sub act_liqpos {
    return mosaik_dialog(too_old_text()) if $TOO_OLD;
    my $addr = good_address(posted_values()->{address}) or return mosaik_dialog('Choose your wallet address first.');
    my $r = chain('ldpositions', { address => $addr }, 60);
    return mosaik_dialog($r->{error} || 'Could not load your positions. Please try again.') unless $r->{ok};
    my (@rows, @extra);
    push @rows, child(label('My positions', 'BODY1BOLD'), align => 'START');
    my $n = 0;
    for my $p (@{ ref $r->{positions} eq 'ARRAY' ? $r->{positions} : [] }) {
        next unless ref $p eq 'HASH' && ($p->{nft} // '') =~ /^[0-9a-f]{64}\z/
                 && !grep { ($p->{$_} // '') !~ /^\d+\z/ } qw(value_erg value_lit claim_erg claim_lit pending_erg pending_lit share_bps);
        $n++;
        my $claimable = $p->{claim_erg} > 0 || $p->{claim_lit} > 0;
        push @rows,
            O(type => 'HorizontalRule', vPadding => 'QUARTER_DEFAULT'),
            child(label('Position ' . substr($p->{nft}, 0, 6) . "\x{2026}  (" . sprintf('%.2f %%', $p->{share_bps} / 100) . ' of the pool)', 'BODY1BOLD'), align => 'START'),
            kv_row('Worth now', erg($p->{value_erg}, 4) . ' + ' . lit($p->{value_lit}, 2), style => 'BODY1'),
            kv_row('Fees to collect', erg($p->{claim_erg}, 6) . ' + ' . lit($p->{claim_lit}, 4), style => 'BODY1'),
            kv_row('Fees still in the pool', erg($p->{pending_erg}, 6) . ' + ' . lit($p->{pending_lit}, 4), style => 'BODY1');
        if ($claimable) {
            push @extra, sign_pair("claim$n", 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&k=claim&t=$p->{nft}&addr=$addr", 'liqPos');
            push @rows, button('Collect fees', "claim$n", style => 'SECONDARY');
        }
        # closing leaves uncollected fees behind for good, so the button only appears once they are collected
        if ($claimable && ($p->{claim_erg} > 1000000 || $p->{claim_lit} > 0)) {
            push @rows, note('Collect the fees before closing: closing a position leaves uncollected fees behind for good.');
        }
        else {
            push @extra, sign_pair("close$n", 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&k=close&t=$p->{nft}&addr=$addr", 'liqPos');
            push @rows, button('Close position', "close$n", style => 'TEXT');
        }
        last if $n >= 10;
    }
    push @rows, note('No liquidity positions on this address. A position shows here once its deposit order has been filled and the LD-NFT is in this wallet.') unless $n;
    push @rows, note('"Fees still in the pool" become collectable after the pool\'s next flush, which Lithos miners do from time to time. '
                     . 'Closing pays out your share of the pool and the 0.6 ERG deposit; a miner fills it like any order.') if $n;
    push @rows, button('Refresh', 'liqPos', style => 'TEXT');
    mosaik_replace(liq_box('liqpos', \@rows), @extra);
}

sub act_status {
    my $nonce = $cgi->url_param('n');
    return mosaik_dialog('Unknown order.') unless defined $nonce && $nonce =~ /^[0-9a-f]{32}\z/;
    my $self  = O(type => 'BackendRequestAction', url => "$API?action=status&n=$nonce", id => 'status', postValues => 'NONE');
    my $again = button('Refresh status', 'status', style => 'SECONDARY');
    my $back  = button('Back to the form', 'clearResult', style => 'TEXT');
    my $row   = sub { child(label($_[0], $_[1] || 'BODY1'), align => 'START') };
    my $rec   = load_record($nonce);
    my @rows;
    if (!$rec || ($rec->{app} // '') ne 'lithos' || !$rec->{tx_id}) {
        @rows = ($row->('Waiting for your wallet', 'BODY1BOLD'),
                 $row->('Your wallet has not picked this order up yet. If you cancelled signing, nothing happened.'), $again, $back);
        return mosaik_replace(result_box(\@rows), $self);
    }
    my $r  = chain('ldstatus', { map { $_ => $rec->{$_} } qw(tx_id watch_box order_box address to) });
    my $st = $r->{ok} ? ($r->{state} // 'unknown') : 'unknown';
    my $what = unit($rec->{from}, $rec->{amount}) . " \x{2192} " . ($rec->{to} eq ERG ? 'ERG' : 'LIT');
    my @extra = ($self, O(type => 'BrowserAction', url => cfg('EXPLORER_WEB_URL', 'https://explorer.ergoplatform.com/en/transactions/') . $rec->{tx_id}, id => 'viewTx'));
    my $view = button('View transaction', 'viewTx', style => 'TEXT');
    if ($st eq 'filled') {
        @rows = ($row->('Order filled', 'BODY1BOLD'), $row->($what),
                 $row->('You received ' . unit($rec->{to}, ($r->{received} // '') =~ /^\d+\z/ ? $r->{received} : $rec->{out}, 4) . '.'), $back);
    }
    elsif ($st eq 'open') {
        @rows = ($row->('Order is waiting for a Lithos miner', 'BODY1BOLD'), $row->($what),
                 $row->('A Lithos miner usually fills it within a few minutes. If the price is below your minimum it keeps waiting. '
                        . 'You can cancel it under "My open orders".'), $again, $view, $back);
    }
    elsif ($st eq 'cancelled') { @rows = ($row->('Order cancelled', 'BODY1BOLD'), $row->($what), $row->('The funds are back on your address.'), $back) }
    elsif ($st eq 'pending')   { @rows = ($row->('Order submitted', 'BODY1BOLD'), $row->($what), $row->('Waiting for the next Ergo block (about two minutes).'), $again, $view) }
    elsif ($st eq 'failed')    { @rows = ($row->('Order was not placed', 'BODY1BOLD'), $row->($what),
                                          $row->('The funds it needed were spent by another transaction first. Nothing was lost. Preview again.'),
                                          button('Preview again', 'preview'), $back) }
    elsif (time - ($rec->{t} || time) < 180) {
        @rows = ($row->('Waiting for the network', 'BODY1BOLD'), $row->($what),
                 $row->('Your transaction has not shown up yet. This can take a moment after signing.'), $again, $back);
    }
    else {
        @rows = ($row->('Not found on the network', 'BODY1BOLD'), $row->($what),
                 $row->("If you cancelled signing, nothing happened. Otherwise check your wallet's history before trying again."), $again, $back);
    }
    mosaik_replace(result_box(\@rows), @extra);
}

sub act_notify {
    notify_json(notifications('lithos', sub {
        my ($it) = @_;
        if (($it->{kind} // '') =~ /^(dep|claim|close|join|reward)\z/) {
            my $st = liq_state($it);
            return (1, 'Your liquidity was added to the LithosDex pool. The LD-NFT in your wallet is your position.') if $it->{kind} eq 'dep' && $st eq 'filled';
            return (1, 'Your LithosDex position was closed and paid out.') if $it->{kind} eq 'close' && $st eq 'filled';
            return (1, 'You joined the Lithos collateral queue.') if $it->{kind} eq 'join' && $st eq 'confirmed';
            return (1) if $st =~ /^(confirmed|cancelled|failed|filled)\z/;
            return (1) if time - ($it->{t} || 0) > 30 * 86400 || ($st eq 'unknown' && time - ($it->{t} || 0) > 86400);
            return (0);
        }
        my $r = chain('ldstatus', { map { $_ => $it->{$_} } qw(tx_id watch_box order_box address to) });
        return (0) unless $r->{ok};
        my $st = $r->{state} // 'unknown';
        return (1, 'Your LIT order was filled: you received '
                   . unit($it->{to}, ($r->{received} // '') =~ /^\d+\z/ ? $r->{received} : $it->{out}, 4) . ' for ' . unit($it->{from}, $it->{amount}) . '.')
            if $st eq 'filled';
        return (1) if $st eq 'cancelled';
        return (1, 'Your LIT order was not placed. Nothing was spent.') if $st eq 'failed';
        return (1) if $st eq 'unknown' && time - ($it->{t} || 0) > 86400;
        return (1) if time - ($it->{t} || 0) > 30 * 86400;    # an order left open for a month: stop asking
        return (0);
    }));
}

# ================================================================= ErgoPay ==

sub ergopay_error { out_json(O(message => $_[0], messageSeverity => 'ERROR')) }

sub act_ergopay {
    my %p = map { $_ => scalar $cgi->param($_) } qw(side a m addr n r k my ms t);
    my $addr = good_address($p{addr}) or return ergopay_error('Invalid request.');
    if (defined $p{r}) {    # cancel: only the owner's key can spend the order, and the helper checks it is theirs
        return ergopay_error('Invalid request.') unless $p{r} =~ /^[0-9a-f]{64}\z/;
        my $r = chain('ldcancel', { order_box => $p{r}, address => $addr }, 60);
        return ergopay_error($r->{error} || 'The cancellation could not be prepared. Please try again.') unless $r->{ok};
        warn "sigmalithos: built cancel tx $r->{tx_id}\n";
        my $what = ($r->{kind} // '') eq 'deposit' ? erg($r->{amount_x} // 0, 4) . ' and ' . lit($r->{amount_y} // 0, 4)
                 : ($r->{kind} // '') eq 'redeem'  ? 'the NFT that owns your position'
                 :                                   unit($r->{from}, $r->{amount});
        return out_json(O(reducedTx => $r->{reduced_tx}, address => $r->{address}, messageSeverity => 'INFORMATION',
                          message => "SigmaLithos: cancel your LithosDex order and take back $what"
                                     . ' with everything else in the order box. Network fee ' . erg($r->{miner_fee}) . '.'));
    }
    my $note = (defined $p{n} && $p{n} =~ /^[0-9a-f]{32}\z/ && note_is_ours($p{n}, 'lithos')) ? $p{n} : undef;
    if (defined $p{k}) {    # liquidity: deposit order, fee collection, closing order
        return ergopay_error('Invalid request.') unless $p{k} =~ /^(dep|claim|close|join|reward)\z/;
        my ($r, $msg);
        if ($p{k} eq 'dep') {
            return ergopay_error('Invalid request.') if grep { !defined $p{$_} || $p{$_} !~ /^[1-9]\d{0,18}\z/ } qw(a my ms);
            $r = chain('ldlpbuild', { amount => $p{a}, max_y => $p{my}, min_shares => $p{ms}, address => $addr }, 60);
            $msg = sub { 'SigmaLithos: place a LithosDex deposit order adding ' . erg($r->{amount_x}) . ' and ' . lit($r->{amount_y}, 4)
                         . ' to the ERG:LIT pool. A Lithos miner fills it (fee ' . erg($r->{executor_fee}) . ') and you receive an LD-NFT that '
                         . 'owns the position; 0.6 ERG is held as the position deposit. You can cancel in SigmaLithos until it is filled.' };
        }
        elsif ($p{k} eq 'join') {
            return ergopay_error('Invalid request.') if grep { !defined $p{$_} || $p{$_} !~ /^(?:0|[1-9]\d{0,18})\z/ } qw(a my);
            $r = chain('lendjoin', { bid => $p{a}, max_permit => $p{my}, address => $addr }, 90);
            $msg = sub { 'SigmaLithos: LEND to the Lithos pool. This locks ' . erg($r->{value}) . ' and ' . lit($r->{permit}, 0) . ' in the collateral queue ('
                         . commas($r->{ahead}) . ' positions ahead). It CANNOT be cancelled and stays locked until a miner uses it. When a miner uses it the LIT '
                         . 'comes back and the block reward is paid to your key.' };
        }
        elsif ($p{k} eq 'reward') {
            $r = chain('lendclaim', { address => $addr }, 90);
            $msg = sub { 'SigmaLithos: collect ' . erg($r->{net}, 4) . " of lending rewards from $r->{boxes} matured reward box" . ($r->{boxes} == 1 ? '' : 'es')
                         . '. ' . erg($r->{reemission}, 0) . ' of what those boxes hold goes to Ergo\'s re-emission contract, as the network requires of every mining reward.' };
        }
        else {
            return ergopay_error('Invalid request.') unless defined $p{t} && $p{t} =~ /^[0-9a-f]{64}\z/;
            if ($p{k} eq 'claim') {
                $r = chain('ldclaim', { nft => $p{t}, address => $addr }, 60);
                $msg = sub { 'SigmaLithos: collect the fees of your LithosDex position, ' . erg($r->{claim_erg}, 6) . ' and ' . lit($r->{claim_lit}, 4)
                             . '. The LD-NFT that owns the position stays in your wallet.' };
            }
            else {
                $r = chain('ldredeem', { nft => $p{t}, address => $addr }, 60);
                $msg = sub { 'SigmaLithos: place an order to CLOSE your LithosDex position. The LD-NFT that owns it goes into the order; a Lithos '
                             . 'miner then pays you about ' . erg($r->{out_erg}, 4) . ' and ' . lit($r->{out_lit}, 2) . ' (fee ' . erg($r->{executor_fee})
                             . ' included). Uncollected fees are left behind. You can cancel in SigmaLithos until it is filled.' };
            }
        }
        return ergopay_error($r->{error} || 'This could not be prepared. Please try again.') unless $r->{ok};
        save_record($note, { app => 'lithos', kind => $p{k}, t => time, tx_id => $r->{tx_id}, watch_box => $r->{watch_box},
                             order_box => $r->{order_box}, address => $r->{address}, claim_erg => $r->{claim_erg}, claim_lit => $r->{claim_lit},
                             net => $r->{net} }) if $note;
        warn "sigmalithos: built $p{k} tx $r->{tx_id}\n";
        return out_json(O(reducedTx => $r->{reduced_tx}, address => $r->{address}, message => $msg->() . ' Network fee ' . erg($r->{miner_fee}) . '.',
                          messageSeverity => $p{k} =~ /^(close|join)\z/ ? 'WARNING' : 'INFORMATION'));
    }
    return ergopay_error('Invalid request.')
        unless defined $p{side} && $p{side} =~ /^(buy|sell)\z/ && !grep { !defined $p{$_} || $p{$_} !~ /^[1-9]\d{0,18}\z/ } qw(a m);
    my $r = chain('ldbuild', { side => $p{side}, amount => $p{a}, min_out => $p{m}, address => $addr }, 60);
    return ergopay_error($r->{error} || 'The order could not be prepared. Please try again.') unless $r->{ok};
    save_record($p{n}, { app => 'lithos', t => time, tx_id => $r->{tx_id}, watch_box => $r->{watch_box}, order_box => $r->{order_box},
                         address => $r->{address}, from => $r->{from}, to => $r->{to}, amount => $r->{amount}, out => $r->{out}, min_out => $r->{min_out} })
        if defined $p{n} && $p{n} =~ /^[0-9a-f]{32}\z/ && note_is_ours($p{n}, 'lithos');
    warn "sigmalithos: built $p{side} order tx $r->{tx_id}\n";    # audit trail, public data only
    out_json(O(
        reducedTx       => $r->{reduced_tx},
        address         => $r->{address},
        message         => 'SigmaLithos: place a LithosDex order to sell ' . unit($r->{from}, $r->{amount}) . ' for about ' . unit($r->{to}, $r->{out}, 4)
                           . ', and never less than ' . unit($r->{to}, $r->{min_out}, 4) . '. A Lithos miner fills it (fee ' . erg($r->{executor_fee})
                           . '), usually within minutes. You can cancel it in SigmaLithos until then. Network fee ' . erg($r->{miner_fee}) . '.',
        messageSeverity => 'INFORMATION',
    ));
}
