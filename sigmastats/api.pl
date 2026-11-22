#!/usr/bin/perl
# SigmaStats - Ergo network figures, a portfolio view and price alerts, in the Ergo wallet.
# A subdirectory app of SigmaSwap (shared config, helper, venv, cache):
#   https://<host>/cgi-bin/sigmaswap/sigmastats/
#
#   Mosaik
#     GET  api.pl                     -> network and market figures
#     POST api.pl?action=port         -> choose an address     ?action=portq -> what it holds, valued in ERG
#     POST api.pl?action=alerts       -> price alerts          ?action=alertset / alertdel&i=
#     GET  api.pl?action=notify       -> wallet notification check ("your alert was reached")
#
# Read-only: this app builds no transactions and asks for no signature. Public, no login.

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/..";
use CGI ();
use JSON::PP ();
use Math::BigInt;
use File::Spec;
use Digest::SHA qw(sha256_hex);
use POSIX qw(strftime);
use AppConfig qw(cfg base_url chain);
use MosaikKit;

use constant APP_VERSION => 2;
use constant ERG         => '0' x 64;
use constant SIGUSD      => '03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04';
use constant MAX_ALERTS  => 10;

$CGI::POST_MAX        = 64 * 1024;
$CGI::DISABLE_UPLOADS = 1;

my $cgi = CGI->new;
print $cgi->header(-type => 'application/json', -charset => 'utf-8', -cache_control => 'no-store');

my $BASE = base_url('sigmastats');
my $API  = "$BASE/api.pl";

my $action = $cgi->url_param('action');
$action = $cgi->param('action') unless defined $action && $action ne '';
$action = '' unless defined $action;

my %mosaik = ('' => \&act_app, home => \&act_home, port => \&act_port, portq => \&act_portq,
              alerts => \&act_alerts, alertset => \&act_alertset, alertdel => \&act_alertdel);

if ($cgi->cgi_error) { die_json('Bad request') }
elsif ($action eq 'notify') {
    eval { act_notify(); 1 } or do { warn "sigmastats: notify failed: $@"; notify_json(undef, 0, 240) };
}
elsif (exists $mosaik{$action}) {
    eval { $mosaik{$action}->(); 1 } or do { warn "sigmastats: '$action' failed: $@"; mosaik_dialog('Something went wrong. Please try again.') };
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

sub read_json {
    my ($file) = @_;
    open(my $fh, '<', $file) or return undef;
    local $/;
    my $d = eval { JSON::PP->new->utf8->decode(scalar <$fh>) };
    close $fh;
    return $d;
}
sub write_json {
    my ($file, $data) = @_;
    my $tmp = "$file.$$";
    open(my $fh, '>', $tmp) or return 0;
    print {$fh} JSON::PP->new->utf8->encode($data);
    close $fh;
    return rename($tmp, $file) ? 1 : (unlink($tmp), 0)[1];
}

sub commas { my ($n) = @_; 1 while $n =~ s/^(\d+)(\d{3})/$1,$2/; return $n }
sub big_unit {    # 550546486408 -> "550.5 G"
    my ($n) = @_;
    for my $u ([1e18, 'E'], [1e15, 'P'], [1e12, 'T'], [1e9, 'G'], [1e6, 'M'], [1e3, 'k']) {
        return sprintf('%.1f %s', $n / $u->[0], $u->[1]) if $n >= $u->[0];
    }
    return sprintf('%.0f ', $n);
}
sub ago {
    my ($ts) = @_;
    my $d = time - $ts;
    return 'just now' if $d < 45;
    return int($d / 60 + 0.5) . ' min ago' if $d < 5400;
    return int($d / 3600 + 0.5) . ' h ago';
}

# ---- prices, from the pool list (reserves as ErgoDex reports them; a few minutes old at most)
# Everything is a whole number of 1e-9 units: ERG per 1 token, or SigUSD per 1 ERG.

sub token_price {    # nanoERG per whole token, from the token's ERG pool
    my ($reg, $id) = @_;
    my $p = $reg->{pair}{ +ERG }{$id} or return undef;
    return undef unless ($p->{xa} // '') =~ /^[1-9]\d*\z/ && ($p->{ya} // '') =~ /^[1-9]\d*\z/;
    # scalar: in list context bdiv returns (quotient, remainder)
    return scalar Math::BigInt->new($p->{xa})->bmul(Math::BigInt->new(10)->bpow($p->{y}{decimals}))->bdiv($p->{ya});
}
sub erg_price {      # 1e-9 SigUSD per whole ERG
    my ($reg) = @_;
    my $p = $reg->{pair}{ +ERG }{ +SIGUSD } or return undef;
    return undef unless ($p->{xa} // '') =~ /^[1-9]\d*\z/ && ($p->{ya} // '') =~ /^[1-9]\d*\z/;
    return scalar Math::BigInt->new($p->{ya})->bmul(Math::BigInt->new(10)->bpow(18 - $p->{y}{decimals}))->bdiv($p->{xa});
}
sub asset_price { my ($reg, $id) = @_; return $id eq ERG ? erg_price($reg) : token_price($reg, $id) }
sub asset_name  { my ($reg, $id) = @_; return $id eq ERG ? 'ERG' : $reg->{tokens}{$id}{label} }
sub price_unit  { my ($id) = @_; return $id eq ERG ? 'SigUSD' : 'ERG' }
sub fmt_price {
    my ($p, $id) = @_;
    my $digits = length("$p");                      # keep about five significant figures
    my $dec = $digits >= 13 ? 2 : $digits >= 10 ? 4 : 9 - ($digits > 5 ? $digits - 5 : 0);
    return fmt_amount("$p", 9, $dec) . ' ' . price_unit($id);
}
sub usd {    # nanoERG -> "12.34" SigUSD
    my ($nano, $ep) = @_;
    return undef unless $ep;
    my $c = Math::BigInt->new("$nano")->bmul($ep)->bdiv(Math::BigInt->new(10)->bpow(16));    # cents
    return commas(sprintf('%d', $c->copy->bdiv(100)->bstr)) . '.' . sprintf('%02d', $c->copy->bmod(100)->bstr);
}

sub netstats {    # cached for a minute: the home screen must not hit the explorer on every open
    my $cd   = cache_dir();
    my $file = $cd ? File::Spec->catfile($cd, 'netstats.json') : undef;
    if ($file && -f $file && time - (stat $file)[9] < 60) { my $d = read_json($file); return $d if ref $d eq 'HASH' && $d->{height} }
    my $r = chain('netstats', {});
    if ($r->{ok} && $r->{height}) { write_json($file, $r) if $file; return $r }
    if ($file && -f $file) { my $d = read_json($file); return $d if ref $d eq 'HASH' && $d->{height} }
    return undef;
}

# ================================================================== alerts ==
# Kept per wallet installation (the random Mosaik-guid the wallet sends; nothing personal):
#   CACHE_DIR/alerts/<hash>.json   the alerts, written only here
#   CACHE_DIR/alerts/<hash>.hits   one line per alert reached, appended only by the bot

sub alert_file {
    my $g = $ENV{HTTP_MOSAIK_GUID};
    return undef unless defined $g && $g =~ /^[A-Za-z0-9._-]{8,80}\z/;
    my $cd = cache_dir() or return undef;
    my $dir = File::Spec->catdir($cd, 'alerts');
    mkdir $dir, 0770 unless -d $dir;
    return (-d $dir && -w $dir) ? File::Spec->catfile($dir, substr(sha256_hex("guid|$g"), 0, 32) . '.json') : undef;
}
sub load_alerts {    # -> (\@alerts, {id => hit})
    my ($file) = @_;
    my $d = -f $file ? read_json($file) : undef;
    my @a = grep { ref $_ eq 'HASH' && ($_->{id} // '') =~ /^[0-9a-f]{8}\z/ && ($_->{asset} // '') =~ /^[0-9a-f]{64}\z/
                   && ($_->{dir} // '') =~ /^(above|below)\z/ && ($_->{price} // '') =~ /^[1-9]\d{0,18}\z/ }
            @{ (ref $d eq 'HASH' && ref $d->{alerts} eq 'ARRAY') ? $d->{alerts} : [] };
    my %hit;
    (my $hf = $file) =~ s/\.json\z/.hits/;
    if (open(my $fh, '<', $hf)) {
        while (my $line = <$fh>) {
            my $h = eval { JSON::PP->new->utf8->decode($line) };
            $hit{ $h->{id} } ||= $h if ref $h eq 'HASH' && defined $h->{id};
        }
        close $fh;
    }
    return (\@a, \%hit);
}
sub save_alerts {
    my ($file, $alerts) = @_;
    (my $hf = $file) =~ s/\.json\z/.hits/;
    if (!@$alerts) { unlink $file, $hf; return 1 }
    return write_json($file, { alerts => $alerts });
}

# ============================================================ Mosaik views ==

sub base_actions {
    return (
        O(type => 'BackendRequestAction', url => "$API?action=home",     id => 'home',     postValues => 'NONE'),
        O(type => 'BackendRequestAction', url => "$API?action=port",     id => 'port',     postValues => 'NONE'),
        O(type => 'BackendRequestAction', url => "$API?action=portq",    id => 'portq',    postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=alerts",   id => 'alerts',   postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=alertset", id => 'alertset', postValues => 'VALID'),
        O(type => 'ReloadAction', id => 'reload'),
    );
}
sub mosaik_action { out_json(O(appVersion => N(APP_VERSION), action => $_[0])) }
sub mosaik_dialog {
    my ($msg) = @_;
    mosaik_action(O(type => 'DialogAction', id => 'msg', message => $msg, positiveButtonText => 'OK'));
}
sub mosaik_replace {
    my ($view, @extra) = @_;
    mosaik_action(O(type => 'ChangeSiteAction', id => 'show', newContent => O(actions => [ base_actions(), @extra ], view => $view)));
}

sub header_rows {
    my ($title, $back) = @_;
    return (
        child(row([
            child(label($title, 'HEADLINE2'), weight => N(1)),
            $back ? button('Back', 'home', style => 'TEXT') : (),
        ]), align => 'JUSTIFY'),
    );
}
sub section { my ($title, @rows) = @_; return child(card(column([ child(label($title, 'BODY1BOLD'), align => 'START'), @rows ],
                                                              padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY') }
sub note    { return child(label($_[0], 'BODY2', color => 'SECONDARY'), align => 'START') }

sub home_view {
    my $reg = registry();
    my $n   = netstats();
    my @net;
    if ($n) {
        push @net, kv_row('Block height', commas($n->{height}));
        push @net, kv_row('Last block', ago($n->{block_ts})) if $n->{block_ts};
        push @net, kv_row('Average block time', sprintf('%.0f s', $n->{avg_block_ms} / 1000)) if $n->{avg_block_ms};
        push @net, kv_row('Hashrate', big_unit($n->{hashrate}) . 'H/s') if $n->{hashrate};
        push @net, kv_row('Difficulty', big_unit($n->{difficulty})) if $n->{difficulty};
        push @net, kv_row('Transactions, 24 h', commas($n->{tx_24h})) if $n->{tx_24h};
        push @net, kv_row('Waiting transactions', commas($n->{mempool})) if defined $n->{mempool};
        push @net, kv_row('ERG in circulation', sprintf('%.2f M of 97.74 M', $n->{supply} / 1e15)) if $n->{supply};
    }
    else { push @net, note('The Ergo explorer is not answering right now.') }

    my @mkt;
    my $ep = erg_price($reg);
    push @mkt, kv_row('ERG price (ErgoDex SigUSD pool)', fmt_price($ep, ERG), style => 'HEADLINE2', color => 'PRIMARY') if $ep;
    my (%seen, $tvl, $pools);
    for my $p (map { values %$_ } values %{ $reg->{pair} }) { next if $seen{ $p->{id} }++; $tvl += $p->{tvl}; $pools++ }
    push @mkt, kv_row('ErgoDex liquidity', '$' . commas(sprintf('%.0f', $tvl)) . " in $pools pools") if $pools;
    my $shown = 0;
    for my $id (@{ $reg->{order} }) {    # the most liquid tokens, priced in ERG
        next if $id eq ERG;
        my $p = token_price($reg, $id) or next;
        push @mkt, kv_row($reg->{tokens}{$id}{label}, fmt_price($p, $id), style => 'BODY1');
        last if ++$shown >= 6;
    }
    push @mkt, note('All prices here are ErgoDex pool prices. The ERG price is what the ERG / SigUSD pool on ErgoDex pays right now. It is not an exchange quote and can differ from one.') if @mkt;
    push @mkt, note('The pool list is unavailable right now.') unless @mkt;

    return column([
        header_rows('SigmaStats'),
        section('Ergo network', @net),
        section('Market', @mkt),
        button('Portfolio', 'port'),
        button('Price alerts', 'alerts', style => 'SECONDARY'),
        button('Refresh', 'home', style => 'TEXT'),
    ], id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

sub port_view {
    my ($addr, $result) = @_;
    return column([
        header_rows('Portfolio', 1),
        section('Address',
            O(type => 'ErgoAddressChooseButton', id => 'address', value => $addr, onValueChanged => 'portq',
              style => $addr ? 'BUTTON_SECONDARY' : undef),
            note('Only looked up, never stored. Nothing is signed.')),
        $result ? @$result : (),
    ], id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

sub alerts_view {
    my ($reg, $v) = @_;
    my $file = alert_file();
    my @kids = header_rows('Price alerts', 1);
    if (!$file) {
        push @kids, section('Not available here', note('Price alerts need the Ergo wallet app: open SigmaStats from its dApps section.'));
        return (column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT'));
    }
    my ($alerts, $hits) = load_alerts($file);
    my (@rows, @extra);
    for my $a (@$alerts) {
        next unless $a->{asset} eq ERG || $reg->{tokens}{ $a->{asset} };
        my $name = asset_name($reg, $a->{asset});
        my $h    = $hits->{ $a->{id} };
        push @extra, O(type => 'BackendRequestAction', url => "$API?action=alertdel&i=$a->{id}", id => "del_$a->{id}", postValues => 'VALID');
        push @rows,
            O(type => 'HorizontalRule', vPadding => 'QUARTER_DEFAULT'),
            child(label("$name " . ($a->{dir} eq 'above' ? 'rises to ' : 'falls to ') . fmt_price($a->{price}, $a->{asset}), 'BODY1BOLD'), align => 'START'),
            child(label($h ? 'Reached ' . strftime('%d %b %H:%M UTC', gmtime($h->{ts} || 0)) . ' at ' . fmt_price($h->{price}, $a->{asset})
                           : 'Waiting. Now ' . (asset_price($reg, $a->{asset}) ? fmt_price(asset_price($reg, $a->{asset}), $a->{asset}) : 'unknown'),
                        'BODY2', color => $h ? 'PRIMARY' : 'SECONDARY'), align => 'START'),
            button('Remove', "del_$a->{id}", style => 'TEXT');
    }
    push @kids, section('Your alerts', @rows ? @rows : note('None yet.'));

    # ---- new alert
    my @assets = (ERG, grep { $_ ne ERG && token_price($reg, $_) } @{ $reg->{order} });
    my $asset  = (defined $v->{asset} && !ref $v->{asset} && grep { $_ eq $v->{asset} } @assets) ? $v->{asset} : ERG;
    my $now    = asset_price($reg, $asset);
    my @form = (
        child(O(type => 'DropDownList', id => 'asset', value => $asset, onValueChanged => 'alerts',
                entries => O(map { $_ => ($_ eq ERG ? 'ERG, in SigUSD (ErgoDex pool)' : $reg->{tokens}{$_}{label} . ', priced in ERG') } @assets)),
              align => 'JUSTIFY'),
    );
    if ($now) {
        push @form,
            child(label('Now: 1 ' . asset_name($reg, $asset) . " = " . fmt_price($now, $asset) . ' (ErgoDex pool price)', 'BODY1', color => 'PRIMARY'), align => 'START'),
            child(O(type => 'DecimalInputField', id => 'price_9', placeholder => 'Tell me at this price (' . price_unit($asset) . ')',
                    onImeAction => 'alertset', imeActionType => 'DONE', minValue => N(1), scale => N(9)), align => 'JUSTIFY'),
            button('Set alert', 'alertset'),
            note('Enter a price above or below the current one. The service checks once a minute; when the price gets there, '
                 . 'your wallet shows a notice the next time it checks this app. An alert fires once.');
    }
    else { push @form, note('No price is available for this right now.') }
    push @kids, section('New alert', @form) if @$alerts < MAX_ALERTS;
    push @kids, note('You have the maximum of ' . MAX_ALERTS . ' alerts. Remove one to add another.') if @$alerts >= MAX_ALERTS;
    return (column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT'), @extra);
}

# ================================================================= actions ==

sub act_app {
    out_json(O(
        manifest => O(
            appName => 'SigmaStats', appDescription => 'Ergo network figures, your portfolio and price alerts.',
            iconUrl => "$BASE/icon.png", appVersion => N(APP_VERSION), targetMosaikVersion => N(2),
            targetCanvasDimension => 'COMPACT_WIDTH', cacheLifetime => N(0),
            notificationCheckUrl => "$API?action=notify",
        ),
        actions => [ base_actions() ],
        view    => home_view(),
    ));
}
sub act_home   { mosaik_replace(home_view()) }
sub act_port   { mosaik_replace(port_view(undef)) }
sub act_alerts { mosaik_replace(alerts_view(registry(), posted_values())) }

sub act_portq {
    my $addr = good_address(posted_values()->{address}) or return mosaik_dialog('Choose an address first.');
    my $r = chain('portfolio', { address => $addr });
    return mosaik_dialog($r->{error} || 'Could not load this address. Please try again.') unless $r->{ok};
    my $reg = registry();
    my $ep  = erg_price($reg);

    my (@priced, @other);
    my $total = Math::BigInt->new(($r->{erg} // '') =~ /^\d+\z/ ? $r->{erg} : 0);
    my $erg   = $total->copy;
    for my $t (@{ $r->{tokens} || [] }) {
        next unless ref $t eq 'HASH' && ($t->{id} // '') =~ /^[0-9a-f]{64}\z/ && ($t->{amount} // '') =~ /^[1-9]\d{0,30}\z/;
        my $known = $reg->{tokens}{ $t->{id} };
        my $price = $known ? token_price($reg, $t->{id}) : undef;
        if ($price) {    # listed on ErgoDex with an ERG pool: name and decimals from the list, valued at the pool price
            my $value = Math::BigInt->new($t->{amount})->bmul($price)->bdiv(Math::BigInt->new(10)->bpow($known->{decimals}));
            $total->badd($value);
            push @priced, [ $known->{label}, fmt_amount($t->{amount}, $known->{decimals}), $value ];
        }
        else {           # anything else: the name is whatever its creator typed, so the id goes with it
            my $dec = ($t->{decimals} // 0) =~ /^\d{1,2}\z/ ? $t->{decimals} + 0 : 0;
            push @other, [ ($known ? $known->{label} : ($t->{name} // 'token') . ' (' . substr($t->{id}, 0, 6) . "\x{2026})"),
                           length($t->{amount}) > 19 ? 'a very large amount' : fmt_amount($t->{amount}, $dec) ];
        }
    }
    @priced = sort { $b->[2]->bcmp($a->[2]) || $a->[0] cmp $b->[0] } @priced;

    my $u = usd($total, $ep);
    my @sum = (kv_row('Total', "\x{2248} " . fmt_amount($total->bstr, 9, 4) . ' ERG', style => 'HEADLINE2', color => 'PRIMARY'));
    push @sum, kv_row('In SigUSD (ErgoDex pool price)', "\x{2248} $u") if defined $u;
    push @sum, note('ERG plus the tokens that have an ERG pool on ErgoDex, at the pool price. Selling a large holding would '
                    . 'fetch less. Liquidity positions and open orders are not included.');
    my @rows = (kv_row('ERG', fmt_amount($erg->bstr, 9, 4)));
    my $n = 0;
    for my $t (@priced) {
        last if ++$n > 40;
        push @rows, kv_row("$t->[1] $t->[0]", "\x{2248} " . fmt_amount($t->[2]->bstr, 9, 4) . ' ERG', style => 'BODY1');
    }
    push @rows, note('and ' . (@priced - 40) . ' more priced tokens') if @priced > 40;
    my @result = (section('Value', @sum), section('Holdings', @rows));
    if (@other) {
        my @o = map { kv_row($_->[0], $_->[1], style => 'BODY1') } @other[0 .. ($#other > 24 ? 24 : $#other)];
        push @o, note('and ' . (@other - 25) . ' more') if @other > 25;
        push @result, section('No price (' . scalar(@other) . ')', @o,
                              note('Tokens without an ERG pool on ErgoDex, including NFTs. Their names are not verified.'));
    }
    push @result, button('Refresh', 'portq', style => 'TEXT');
    mosaik_replace(port_view($addr, \@result));
}

sub act_alertset {
    my $file = alert_file() or return mosaik_dialog('Price alerts need the Ergo wallet app.');
    my $reg  = registry();
    my $v    = posted_values();
    my $asset = $v->{asset};
    return mosaik_dialog('Choose what to watch.')
        unless defined $asset && !ref $asset && $asset =~ /^[0-9a-f]{64}\z/ && ($asset eq ERG || $reg->{tokens}{$asset});
    my $now = asset_price($reg, $asset) or return mosaik_dialog('No price is available for this right now.');
    my $p = $v->{price_9};
    return mosaik_dialog('Enter the price you want to be told about.') unless defined $p && !ref $p && "$p" =~ /^[1-9]\d{0,18}\z/;
    my $cmp = Math::BigInt->new("$p")->bcmp($now);
    return mosaik_dialog('That is the current price. Enter one above or below it.') if $cmp == 0;

    my ($alerts, $hits) = load_alerts($file);
    return mosaik_dialog('You have the maximum of ' . MAX_ALERTS . ' alerts. Remove one first.') if @$alerts >= MAX_ALERTS;
    if (!-f $file) {    # anyone can create an installation id, so the directory is bounded
        (my $dir = $file) =~ s{/[^/]+\z}{};
        if (opendir(my $dh, $dir)) { my $n = grep { /\.json\z/ } readdir $dh; closedir $dh; return mosaik_dialog('Price alerts are full right now. Please try later.') if $n >= 20000 }
    }
    my $id = substr(new_nonce() // '', 0, 8);
    return mosaik_dialog('Something went wrong. Please try again.') unless $id =~ /^[0-9a-f]{8}\z/;
    push @$alerts, { id => $id, asset => $asset, dir => ($cmp > 0 ? 'above' : 'below'), price => "$p", t => time };
    return mosaik_dialog('The alert could not be saved. Please try again.') unless save_alerts($file, $alerts);
    mosaik_replace(alerts_view($reg, { asset => $asset }));
}

sub act_alertdel {
    my $file = alert_file() or return mosaik_dialog('Price alerts need the Ergo wallet app.');
    my $id   = $cgi->url_param('i') // '';
    my ($alerts) = load_alerts($file);
    save_alerts($file, [ grep { $_->{id} ne $id } @$alerts ]);
    mosaik_replace(alerts_view(registry(), posted_values()));
}

sub act_notify {
    my $file = alert_file() or return notify_json(undef, 0, 240);
    my ($alerts, $hits) = load_alerts($file);
    my $reg = registry();
    my ($best, $waiting) = (undef, 0);
    for my $a (@$alerts) {
        my $h = $hits->{ $a->{id} };
        if (!$h) { $waiting++; next }
        next unless ($h->{ts} // 0) =~ /^\d+\z/ && time - $h->{ts} < 3 * 86400 && ($h->{price} // '') =~ /^\d+\z/;
        next unless $a->{asset} eq ERG || $reg->{tokens}{ $a->{asset} };
        $best = [ $a, $h ] if !$best || $h->{ts} > $best->[1]{ts};
    }
    return notify_json(undef, 0, $waiting ? 5 : 240) unless $best;
    my ($a, $h) = @$best;
    notify_json('Price alert: ' . asset_name($reg, $a->{asset}) . ($a->{dir} eq 'above' ? ' rose to ' : ' fell to ')
                . fmt_price($h->{price}, $a->{asset}) . ' (your alert: ' . fmt_price($a->{price}, $a->{asset}) . ').',
                $h->{ts}, $waiting ? 5 : 240);
}
