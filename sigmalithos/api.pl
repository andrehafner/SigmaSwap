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

use constant APP_VERSION => 3;
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

my %mosaik = ('' => \&act_app, home => \&act_home, dash => \&act_dash, mine => \&act_mine,
              trade => \&act_trade, quote => \&act_quote, orders => \&act_orders, status => \&act_status);

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
        $choice->('Trade LIT', 'Buy LIT with ERG or sell LIT for ERG on LithosDex, the exchange built into Lithos.',
                  'You place an order; a Lithos miner fills it at the pool price, never below your minimum. You can cancel it until then. '
                  . 'Needs Ergo wallet app version 2.5.1 or newer.', 'Trade LIT', 'goTrade', 'SECONDARY'),
        label('An independent app, not made by the Lithos project. It cannot set up or control a miner: that needs the Lithos client on your own computer.',
              'BODY2', color => 'SECONDARY', talign => 'CENTER'),
        button('lithosprotocol.org', 'openLithos', style => 'TEXT'),
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
    push @kids, label('Orders are filled by Lithos miners in the blocks they mine, about every 45 minutes on average, sometimes much longer. '
                      . 'Nothing here is instant.', 'BODY2', color => 'SECONDARY', talign => 'CENTER');
    # LithosDex contracts use Ergo 6.0 operations. A wallet build without 6.0 support cannot read the
    # order box and stops with "Cannot create ErgoAddress from unparsed ergo tree" before signing.
    push @kids, label('Needs Ergo wallet app version 2.5.1 or newer. If signing stops with an error about an "unparsed ergo tree", '
                      . 'your wallet is older and cannot read the contract format LithosDex uses: nothing was sent. Update the wallet and try again.',
                      'BODY2', color => 'SECONDARY', talign => 'CENTER');
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
sub act_trade { mosaik_replace(trade_view(trade_state(posted_values()))) }

sub act_quote {
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
             . 'minimum. That takes about 45 minutes on average and can take hours. Other trades can move the price before then, in either '
             . 'direction. Until it is filled you can cancel it under "My open orders" and get everything back except the network fees. '
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
            child(label('Your Ergo wallet app must be version 2.5.1 or newer to sign this. Older versions stop with an error.',
                        'BODY2BOLD', color => 'PRIMARY'), weight => N(1)),
        ], spacing => 'HALF_DEFAULT'), align => 'JUSTIFY');
        push @rows, button($buy ? 'Place order: buy LIT' : 'Place order: sell LIT', 'pay');
    }
    push @rows, button('Refresh', 'preview', style => 'TEXT');
    mosaik_replace(result_box(\@rows), @extra);
}

sub act_orders {
    my $addr = good_address(posted_values()->{address}) or return mosaik_dialog('Choose your wallet address first.');
    my $r = chain('ldorders', { address => $addr }, 60);
    return mosaik_dialog($r->{error} || 'Could not load your orders. Please try again.') unless $r->{ok};
    my (@rows, @extra);
    push @rows, child(label('Open orders', 'BODY1BOLD'), align => 'START');
    my $n = 0;
    for my $o (@{ ref $r->{orders} eq 'ARRAY' ? $r->{orders} : [] }) {
        next unless ref $o eq 'HASH' && ($o->{box_id} // '') =~ /^[0-9a-f]{64}\z/ && ($o->{amount} // '') =~ /^\d+\z/
                 && ($o->{min_out} // '') =~ /^\d+\z/ && ($o->{from} // '') =~ /^[0-9a-f]{64}\z/ && ($o->{to} // '') =~ /^[0-9a-f]{64}\z/;
        $n++;
        push @extra, O(type => 'ErgoPayAction', id => "cancel$n", onFinished => 'orders',
                       url => 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&r=$o->{box_id}&addr=$addr");
        push @rows,
            O(type => 'HorizontalRule', vPadding => 'QUARTER_DEFAULT'),
            child(label('Sell ' . unit($o->{from}, $o->{amount}) . ' for at least ' . unit($o->{to}, $o->{min_out}, 4), 'BODY1'), align => 'START'),
            button('Cancel and take it back', "cancel$n", style => 'SECONDARY');
        last if $n >= 20;
    }
    push @rows, note('No open LIT orders on this address. Filled and cancelled orders disappear from this list.') unless $n;
    push @rows, note('An order still here is waiting for a Lithos miner to mine a block, or for the price to come back above its minimum.') if $n;
    push @rows, button('Refresh', 'orders', style => 'TEXT');
    mosaik_replace(O(type => 'Box', id => 'orders',
                     children => [ child(card(column(\@rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]), @extra);
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
                 $row->('It is filled when a Lithos miner next mines a block, about every 45 minutes on average. If the price is below '
                        . 'your minimum it keeps waiting. You can cancel it under "My open orders".'), $again, $view, $back);
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
    my %p = map { $_ => scalar $cgi->param($_) } qw(side a m addr n r);
    my $addr = good_address($p{addr}) or return ergopay_error('Invalid request.');
    if (defined $p{r}) {    # cancel: only the owner's key can spend the order, and the helper checks it is theirs
        return ergopay_error('Invalid request.') unless $p{r} =~ /^[0-9a-f]{64}\z/;
        my $r = chain('ldcancel', { order_box => $p{r}, address => $addr }, 60);
        return ergopay_error($r->{error} || 'The cancellation could not be prepared. Please try again.') unless $r->{ok};
        warn "sigmalithos: built cancel tx $r->{tx_id}\n";
        return out_json(O(reducedTx => $r->{reduced_tx}, address => $r->{address}, messageSeverity => 'INFORMATION',
                          message => 'SigmaLithos: cancel your LithosDex order and take back ' . unit($r->{from}, $r->{amount})
                                     . ' with everything else in the order box. Network fee ' . erg($r->{miner_fee}) . '.'));
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
                           . '); this can take an hour or more. You can cancel it in SigmaLithos until then. Network fee ' . erg($r->{miner_fee}) . '.',
        messageSeverity => 'INFORMATION',
    ));
}
