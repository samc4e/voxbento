"""Word lists for glossary suggestions (#1172): ordinary words that are never worth a glossary entry.

All entries are casefolded. ``COMMON_WORDS`` is a few thousand everyday English words (plus
the vocabulary every talk and every VoxBento room uses), so a word missing from it is
"uncommon". ``STOPWORDS`` holds function words for the languages talks are most often
given in, so a capitalised "Die" or "Les" at the start of a sentence is not taken for a name.
"""

from __future__ import annotations


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


ENGLISH_STOPWORDS = _words(
    """
    a about above after again against all almost alone along already also although always am among an and
    another any anybody anyone anything anywhere are aren't around as at be became because become becomes
    been before being below beside besides between beyond both but by can cannot can't could couldn't did
    didn't do does doesn't doing don't done down during each either else enough etc even ever every
    everybody everyone everything everywhere few for from further had hadn't has hasn't have haven't having
    he he'd he'll he's her here here's hers herself him himself his how how's however i i'd i'll i'm i've if
    in into is isn't it it's its itself just let let's may maybe me might mine more most much must mustn't
    my myself neither never no nobody none nor not nothing now nowhere of off often on once one only onto
    or other others otherwise ought our ours ourselves out over own per perhaps quite rather really same
    shall shan't she she'd she'll she's should shouldn't since so some somebody someone something sometimes
    somewhere still such than that that's the their theirs them themselves then there there's therefore
    these they they'd they'll they're they've this those though through throughout thus to together too
    toward towards under until unless up upon us very via was wasn't we we'd we'll we're we've were weren't
    what what's whatever when when's whenever where where's whereas wherever whether which while who who's
    whoever whole whom whose why why's will with within without won't would wouldn't yes yet you you'd
    you'll you're you've your yours yourself yourselves
    """
)

_OTHER_STOPWORDS = _words(
    """
    aber alle allem allen aller alles als also am an andere anderen auch auf aus bei beim bin bis bist da
    damit dann das dass dein deine dem den denn der des dessen deshalb die dies diese diesem diesen dieser
    dieses doch dort du durch ein eine einem einen einer eines er es etwa euch euer für gegen hat hatte
    haben hier hin hinter ich ihm ihn ihr ihre im in indem ins ist ja jede jedem jeden jeder jedes jetzt
    kann kein keine können mal man mehr mein meine mit muss nach nicht nichts noch nun nur ob oder ohne
    sehr sein seine sich sie sind so solche soll sondern sonst über um und uns unser unter vom von vor war
    waren warum was weil welche welcher wenn wer werden wie wieder wir wird wo zu zum zur zwischen

    à au aux avec ce ceci cela celle celles celui ces cet cette chaque comme dans de des du elle elles en
    encore est et été être eu il ils je la le les leur leurs lui ma mais me même mes moi mon ne nos notre
    nous on ont ou où par pas pour qu que qui sa sans se ses si son sont sur ta te tes toi ton tous tout
    toute toutes très tu un une vos votre vous y c'est

    al algo algunos ante antes como con contra cual cuando del desde donde durante el ella ellas ellos
    entre era es esa ese eso esta este esto estos fue ha hay las lo los más mi muy nada ni nos nosotros
    otra otro para pero poco por porque quien ser sin sobre su sus también tan tiene todo todos una uno
    unos ya yo

    alla alle che chi ci con dal dalla dei del della delle di gli ho il io lei loro ma ne nei nel nella
    non noi più quando quello questa questo si sono sua suo sul tra voi

    ao aos com da das do dos ela ele em essa esse eu foi isso isto já mas mesmo meu minha muito na nas
    não nós os pela pelo sem seu também tem um uma uns você

    aan bij dat deze dit door een er geen heb heeft hem het hij hoe hun ik je maar met mijn naar niet nog
    nu om omdat ons ook op te tot uit van veel voor wat wel werd wie wij wordt zal ze zich zij zijn
    """
)

#: Days and months are capitalised in English but are never glossary terms.
_CALENDAR_WORDS = _words(
    """
    monday tuesday wednesday thursday friday saturday sunday january february march april may june july
    august september october november december
    """
)

STOPWORDS = ENGLISH_STOPWORDS | _OTHER_STOPWORDS | _CALENDAR_WORDS

COMMON_WORDS = _words(
    """
    ability able about above absolutely accept access according account across act action active activity
    actual actually add added addition additional address admit adult advance advantage advice affect
    afford afraid after afternoon again against age agency agent ago agree agreement ahead aim air allow
    almost alone along already alright also alternative although always amazing among amount analysis
    analyze ancient and anger angle angry animal announce annual another answer anybody anymore anyone
    anything anyway apart apparent appear apple application apply appreciate approach appropriate approve
    area argue argument arm army around arrange arrive art article artist as aside ask aspect assume at
    attack attempt attend attention attitude audience author authority automatic available average avoid
    award aware away awesome awful baby back background bad bag balance ball band bank bar base basic
    basically basis battle be beach bear beat beautiful beauty because become bed beer before begin
    beginning behave behavior behaviour behind belief believe belong below benefit best better between
    beyond big bill billion bird birth bit black blame block blog blood blue board boat body book border
    born borrow boss both bottle bottom box boy brain branch brand bread break breakfast bridge brief
    bright brilliant bring broad brother brown browser budget bug build building built burn business busy
    but button buy by cake call calm camera camp campaign can cancel cancer candidate cap capable capacity
    capital captain caption captions car card care career careful carry case cash cat catch category cause
    celebrate cell center central centre century certain certainly chain chair challenge champion chance
    change channel chapter character charge chart chat cheap check cheese chicken chief child choice
    choose church circle citizen city civil claim class classic clean clear clearly click client climate
    climb clock close closed cloud club coach coast code coffee cold collect collection college color
    colour column combine come comfortable command comment commercial common communicate communication
    community company compare comparison competition complain complete completely complex computer concept
    concern concert conclusion condition conference confident confirm conflict connect connection consider
    constant consumer contact contain content contest context continue contract contrast contribute
    control convert cook cool copy core corner correct cost could council count counter country couple
    courage course court cover crash crazy create creative credit crime crisis critical cross crowd cry
    culture cup current currently customer cut cycle daily damage dance danger dark data database date
    daughter day dead deal dear death debate decade decide decision deep default defend defense define
    definitely degree delay delete deliver delivery demand demo demonstrate department depend depth
    describe description design desk despite destroy detail determine develop developer development device
    die difference different difficult dig digital dinner direct direction directly director dirty disable
    discover discuss discussion disease display distance divide do doctor document dog dollar door double
    doubt down download dozen draft drag draw dream dress drink drive driver drop drug dry due during duty
    each eager ear early earn earth ease easily east easy eat economic economy edge edit edition editor
    education effect effective effort egg eight either elect election electric element else email
    emergency emotion employee employer empty enable end enemy energy engine engineer engineering enjoy
    enough ensure enter entire entirely entry environment equal equipment error escape especially essay
    essential establish estimate even evening event eventually ever every everybody everyday everyone
    everything evidence evil exact exactly exam example excellent except exchange excited exciting excuse
    executive exercise exist expand expect expensive experience experiment expert explain explore express
    extend extra extreme eye face fact factor fail failure fair fall false familiar family famous fan
    fantastic far farm fashion fast fat father fault favorite favourite fear feature features federal fee
    feed feedback feel feeling fellow female fetch few field fight figure file fill film final finally
    finance financial find fine finger finish fire firm first fish fit five fix flag flat flight floor flow
    flower fly focus folder folk follow food foot football for force foreign forest forever forget form
    formal format former forward four frame free freedom fresh friend friendly from front fruit full fully
    fun function fund funny future gain game gap garden gas gate gather general generally generate
    generation gentle get gift girl give glad glass global go goal god gold golden good goodbye govern
    government grab grade grand grant graph great green grey gray ground group grow growth guarantee guard
    guess guest guide guy habit hair half hall hand handle hang happen happy hard hardware hardly hate
    have head headline health healthy hear heart heat heavy hello help helpful her here hero hey hi hide
    high highlight highly hill him himself hire his history hit hold hole holiday home honest hope horse
    hospital host hot hotel hour house how however huge human humor hundred hungry hunt hurt husband ice
    idea ideal identify identity if ignore ill illegal image imagine immediately impact important
    impossible impress improve improvement in include including income increase incredible indeed
    independent index indicate individual industry influence inform information initial inner input
    inside insight install instance instead institute instruction intelligence intend interest interested
    interesting internal international internet interpret interpretation interpreter interview into
    introduce introduction invest investment invite involve iron island issue it item its itself job join
    joke journal journey joy judge jump junior just justice keen keep key keynote kick kid kill kind king
    kitchen knee knife know knowledge lab label labour labor lack lady lake land language large largely
    last late later latest laugh launch law lawyer lay layer lazy lead leader leadership leading learn
    learning least leave left leg legal length less lesson let letter level library lie life lift light
    like likely limit line link list listen listener little live living load loan local location lock
    log login logo long look loose lose loss lost lot loud love lovely low luck lucky lunch machine mad
    magazine magic mail main mainly maintain major majority make male man manage management manager manner
    manual many map mark market marketing marriage mass massive master match material math matter maximum
    may maybe mean meaning measure meat media medical medium meet meeting member memory mental mention menu
    mere message metal method middle might mile military milk million mind minimum minor minute mirror
    miss mission mistake mix mobile mode model modern moment money monitor month mood moon moral more
    morning mostly mother motion motivation motor mountain mouse mouth move movement movie much multiple
    music must mutual name narrow nation national native natural nature near nearly neat necessary neck
    need negative neighbor neighbour nervous net network never new news newspaper next nice night nine
    nobody noise none normal normally north nose not note notes nothing notice novel now number nurse
    object obvious obviously occasion occur ocean odd of off offer office officer official often oh oil ok
    okay old on once one online only onto open opening operate operation opinion opportunity oppose
    option or orange order ordinary organization organisation organize original other otherwise ought our
    out outcome output outside over overall overview own owner pack package page pain paint pair panel
    paper parent park part participant particular particularly partly partner party pass passage passenger
    passion password past path patient pattern pause pay peace peak pen people per percent perfect
    perfectly perform performance perhaps period permanent permission person personal personally phase
    phone photo phrase physical pick picture piece pilot pink pitch place plain plan plane planet plant
    platform play player please pleasure plenty plus pocket point police policy polite political politics
    pool poor pop popular population position positive possible possibly post pot potential pound power
    powerful practical practice praise pray prefer prepare presence present presentation presenter
    president press pressure pretty prevent previous previously price pride primary prime prince principle
    print prior priority private prize probably problem procedure process produce product production
    profession professional professor profile profit program programme programming progress project
    promise promote proof proper properly property proposal propose protect protection proud prove
    provide public publish pull purchase pure purpose push put quality quantity quarter queen question
    questions quick quickly quiet quit quite quote race radio rain raise random range rapid rare rarely
    rate rather raw reach react read reader reading ready real reality realize realise really reason
    reasonable recall receive recent recently recognize recognise recommend record recover red reduce
    refer reference reflect reform refuse regard region regular regularly reject relate related relation
    relationship relative relatively relax release relevant relief religion rely remain remark remember
    remind remote remove rent repair repeat replace reply report represent request require requirement
    research reserve resolve resource respect respond response responsibility responsible rest restaurant
    result results retain return reveal review revolution reward rich ride right ring rise risk river road
    rock role roll room root rough round route routine row royal rule run rush sad safe safety sale salt
    same sample sand save say scale scene schedule scheme school science scientist score screen script
    sea search season seat second secret section sector secure security see seed seek seem select self
    sell send senior sense sensitive sentence separate series serious seriously serve server service
    session set setting settings settle setup seven several severe sex shake shall shape share sharp she
    sheet shift shine ship shirt shock shoe shoot shop short shot should shoulder shout show shut sick
    side sign signal significant silent silly silver similar simple simply since sing single sister sit
    site situation six size skill skin sky sleep slide slides slight slightly slow slowly small smart
    smell smile smoke smooth snow so social society soft software soil solid solution solve some somebody
    someone something somewhat son song soon sorry sort soul sound source south space speak speaker special
    specific speech speed spell spend spirit split spoken sport spot spread spring square staff stage
    stand standard star start state statement station status stay steady steal step stick still stock
    stone stop store storm story straight strange stranger strategy stream streaming street strength
    stress stretch strict strike string strong structure struggle student studio study stuff stupid style
    subject submit subtitle subtitles succeed success successful such sudden suddenly suffer sugar suggest
    suit summary summer sun super supply support suppose sure surface surprise survey survive switch
    symbol system table tag take takeaway tale talent talk tall target task taste tax tea teach teacher
    team tear tech technical technique technology telephone television tell temperature template ten tend
    term terms terrible test testing text than thank thanks that the theater theatre their them theme then
    theory there these they thick thin thing think third this those though thought thousand threat three
    through throw thus ticket tie tight till time tiny tip tired title to today together toilet tomorrow
    tone tonight too tool tools top topic total touch tough tour toward town toy track trade tradition
    traditional traffic train training transfer transform translate translation transport travel treat
    treatment tree trend trial trick trip trouble truck true truly trust truth try tune turn twelve twice
    two type typical typically ugly unable uncle under understand understanding unfortunately uniform
    union unique unit universe university unknown unless unlike until unusual up update upload upon upper
    upset urban urge us use used useful user usual usually valid valley valuable value variety various
    vary vast version very via video view village visit visitor visual voice volume vote wage wait wake
    walk wall want war warm warn wash waste watch water wave way we weak wealth weapon wear weather web
    website wedding week weekend weekly weigh weight weird welcome well west western wet what whatever
    wheel when where whether which while white who whole whom whose why wide widely wife wild will
    willing win wind window windows wine wing winner winter wire wise wish with within without woman
    wonder wonderful wood word work worker workflow workshop world worried worry worse worst worth would
    wow write writer writing wrong yard yeah year yellow yes yesterday yet you young your yourself youth
    zero zone

    eleven hundred thousand million billion

    agenda appendix contents demo goals introduction motivation outline recap references roadmap
    speakers sponsor sponsors summary takeaways thanks questions welcome

    app apps audio backup bandwidth beta button chat checkbox client clients cloud config configuration
    dashboard desktop developers devices digital display domain dropdown email emails feature files
    folders headphones homepage install keyboard laptop library link links login logout microphone
    mobile offline online open plugin popup profile realtime screen sessions sign signup source tablet
    tab tabs upgrade url users video videos webpage wifi
    """
)
