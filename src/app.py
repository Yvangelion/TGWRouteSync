import json
import boto3
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set, Optional, Any
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
DYNAMODB_TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'TGWRouteSyncState')
TAG_KEY = 'TGWRouteSync'
TAG_VALUE = 'enabled'

# AWS clients
networkmanager_client = boto3.client('networkmanager')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(DYNAMODB_TABLE_NAME)

# Cache for regional EC2 clients
_ec2_clients: Dict[str, Any] = {}

def get_ec2_client(region: str):
    """Get or create an EC2 client for a specific region."""
    if region not in _ec2_clients:
        logger.debug(f"Creating new EC2 client for region: {region}")
        _ec2_clients[region] = boto3.client('ec2', region_name=region)
    return _ec2_clients[region]

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for TGW Route Sync.
    
    Logic:
    1. Discover TGWs with TGWRouteSync=enabled tag
    2. For each TGW route table, compare TGW routes (source) with VPC routes (target)
    3. If they match → no action (false positive)
    4. If they differ → sync VPC routes to match TGW routes IMMEDIATELY
    
    NO COOLDOWN - syncs happen immediately when routes differ.
    """
    logger.info("=" * 60)
    logger.info("Starting TGW Route Sync Lambda execution")
    logger.info(f"Configuration: DYNAMODB_TABLE={DYNAMODB_TABLE_NAME}")
    logger.info("=" * 60)

    results = {
        'processed_tgws': [],
        'skipped_tgws': [],
        'errors': [],
        'total_routes_added': 0,
        'total_routes_removed': 0
    }

    try:
        # Discover enabled TGWs via Global Networks
        enabled_tgws = discover_enabled_tgws()

        if not enabled_tgws:
            logger.warning("No TGWs found with TGWRouteSync=enabled tag")
            return {
                'statusCode': 200,
                'body': json.dumps({'message': 'No enabled TGWs found', 'results': results})
            }

        logger.info(f"Discovered {len(enabled_tgws)} enabled TGW(s) across regions")
        for tgw in enabled_tgws:
            logger.info(f"  - {tgw['tgw_id']} in {tgw['region']}")

        # Process each enabled TGW
        for tgw_info in enabled_tgws:
            tgw_id = tgw_info['tgw_id']
            global_network_id = tgw_info['global_network_id']
            region = tgw_info['region']

            logger.info("-" * 40)
            logger.info(f"Processing TGW: {tgw_id}")
            logger.info(f"  Region: {region}")
            logger.info(f"  Global Network: {global_network_id}")

            try:
                tgw_result = process_tgw(tgw_id, global_network_id, region)

                if tgw_result['skipped']:
                    results['skipped_tgws'].append({
                        'tgw_id': tgw_id,
                        'region': region,
                        'reason': tgw_result['skip_reason']
                    })
                else:
                    results['processed_tgws'].append({
                        'tgw_id': tgw_id,
                        'region': region,
                        'routes_added': tgw_result['routes_added'],
                        'routes_removed': tgw_result['routes_removed']
                    })
                    results['total_routes_added'] += tgw_result['routes_added']
                    results['total_routes_removed'] += tgw_result['routes_removed']

            except Exception as e:
                logger.error(f"Error processing TGW {tgw_id}: {str(e)}", exc_info=True)
                results['errors'].append({
                    'tgw_id': tgw_id,
                    'region': region,
                    'error': str(e)
                })

        logger.info("=" * 60)
        logger.info("Execution complete")
        logger.info(f"  Processed: {len(results['processed_tgws'])} TGW(s)")
        logger.info(f"  Skipped: {len(results['skipped_tgws'])} TGW(s)")
        logger.info(f"  Errors: {len(results['errors'])}")
        logger.info(f"  Routes added: {results['total_routes_added']}")
        logger.info(f"  Routes removed: {results['total_routes_removed']}")
        logger.info("=" * 60)

        return {
            'statusCode': 200,
            'body': json.dumps({'message': 'TGW Route Sync completed', 'results': results})
        }

    except Exception as e:
        logger.error(f"Fatal error in Lambda execution: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }

def discover_enabled_tgws() -> List[Dict[str, str]]:
    """
    Discover TGWs enabled for route sync via Global Network tags.
    Works across ANY region - extracts region from TGW ARN.
    """
    enabled_tgws = []

    logger.info("Discovering Global Networks with TGWRouteSync=enabled tag")

    try:
        paginator = networkmanager_client.get_paginator('describe_global_networks')

        for page in paginator.paginate():
            for global_network in page.get('GlobalNetworks', []):
                global_network_id = global_network['GlobalNetworkId']
                gn_tags = {tag['Key']: tag['Value'] for tag in global_network.get('Tags', [])}

                if gn_tags.get(TAG_KEY) != TAG_VALUE:
                    logger.debug(f"Skipping Global Network {global_network_id}: tag not enabled")
                    continue

                logger.info(f"Found enabled Global Network: {global_network_id}")

                tgws_in_network = get_tgws_for_global_network(global_network_id)

                for tgw_info in tgws_in_network:
                    tgw_id = tgw_info['tgw_id']
                    tgw_region = tgw_info['region']

                    logger.info(f"Checking TGW {tgw_id} in region {tgw_region}")

                    if is_tgw_enabled(tgw_id, tgw_region):
                        enabled_tgws.append({
                            'tgw_id': tgw_id,
                            'global_network_id': global_network_id,
                            'region': tgw_region
                        })
                        logger.info(f"  ✓ TGW {tgw_id} is enabled for route sync")
                    else:
                        logger.info(f"  ✗ TGW {tgw_id} does not have TGWRouteSync=enabled tag")

    except ClientError as e:
        logger.error(f"Error discovering Global Networks: {str(e)}")
        raise

    return enabled_tgws

def get_tgws_for_global_network(global_network_id: str) -> List[Dict[str, str]]:
    """Get all Transit Gateways registered to a Global Network."""
    tgws = []

    try:
        paginator = networkmanager_client.get_paginator('get_transit_gateway_registrations')

        for page in paginator.paginate(GlobalNetworkId=global_network_id):
            for registration in page.get('TransitGatewayRegistrations', []):
                state = registration.get('State', {}).get('Code')

                if state == 'AVAILABLE':
                    tgw_arn = registration['TransitGatewayArn']
                    arn_parts = tgw_arn.split(':')
                    region = arn_parts[3]
                    tgw_id = arn_parts[5].split('/')[-1]

                    tgws.append({
                        'tgw_id': tgw_id,
                        'region': region
                    })
                    logger.debug(f"Found registered TGW: {tgw_id} in region {region}")

    except ClientError as e:
        logger.error(f"Error getting TGWs for Global Network {global_network_id}: {str(e)}")
        raise

    return tgws

def is_tgw_enabled(tgw_id: str, region: str) -> bool:
    """Check if a Transit Gateway has the TGWRouteSync=enabled tag."""
    try:
        ec2 = get_ec2_client(region)

        response = ec2.describe_transit_gateways(
            TransitGatewayIds=[tgw_id]
        )

        if response.get('TransitGateways'):
            tgw = response['TransitGateways'][0]
            tags = {tag['Key']: tag['Value'] for tag in tgw.get('Tags', [])}
            is_enabled = tags.get(TAG_KEY) == TAG_VALUE
            return is_enabled
        else:
            logger.warning(f"No TGW found with ID {tgw_id} in region {region}")
            return False

    except ClientError as e:
        logger.error(f"Error checking TGW {tgw_id} tags in region {region}: {str(e)}")
        return False

def process_tgw(tgw_id: str, global_network_id: str, region: str) -> Dict[str, Any]:
    """Process a single Transit Gateway for route synchronization."""
    result = {
        'skipped': False,
        'skip_reason': None,
        'routes_added': 0,
        'routes_removed': 0,
        'route_tables_processed': []
    }

    tgw_route_tables = get_tgw_route_tables(tgw_id, region)

    if not tgw_route_tables:
        logger.warning(f"No route tables found for TGW {tgw_id} in {region}")
        result['skipped'] = True
        result['skip_reason'] = 'No route tables found'
        return result

    logger.info(f"Found {len(tgw_route_tables)} route table(s) for TGW {tgw_id}")

    for rt_id in tgw_route_tables:
        rt_result = process_tgw_route_table(tgw_id, rt_id, global_network_id, region)
        result['route_tables_processed'].append(rt_result)

        if not rt_result['skipped']:
            result['routes_added'] += rt_result['routes_added']
            result['routes_removed'] += rt_result['routes_removed']

    if all(rt['skipped'] for rt in result['route_tables_processed']):
        result['skipped'] = True
        result['skip_reason'] = 'All route tables already in sync'

    return result

def process_tgw_route_table(tgw_id: str, tgw_route_table_id: str,
                           global_network_id: str, region: str) -> Dict[str, Any]:
    """
    Process a single TGW route table.
    
    LOGIC (NO COOLDOWN):
    1. Get TGW routes (SOURCE OF TRUTH)
    2. Get VPC routes for each tagged VPC route table
    3. Compare TGW routes vs VPC routes
    4. If match → FALSE POSITIVE (no sync needed)
    5. If mismatch → SYNC IMMEDIATELY to make VPC match TGW
    """
    result = {
        'route_table_id': tgw_route_table_id,
        'skipped': False,
        'skip_reason': None,
        'routes_added': 0,
        'routes_removed': 0,
        'vpc_route_tables_checked': 0,
        'vpc_route_tables_synced': 0
    }

    logger.info(f"Processing TGW route table: {tgw_route_table_id}")

    # Step 1: Get current routes from TGW route table (SOURCE OF TRUTH)
    tgw_routes = get_tgw_routes(tgw_route_table_id, region)
    tgw_cidrs = set(tgw_routes.keys())

    logger.info(f"TGW route table {tgw_route_table_id} has {len(tgw_cidrs)} routes (SOURCE OF TRUTH)")
    for cidr in sorted(tgw_cidrs):
        logger.debug(f"  TGW Route: {cidr}")

    # Step 2: Get VPC attachments and their route tables
    vpc_attachments = get_vpc_attachments(tgw_route_table_id, region)

    if not vpc_attachments:
        logger.info(f"No VPC attachments found for {tgw_route_table_id}")
        result['skipped'] = True
        result['skip_reason'] = 'No VPC attachments'
        return result

    logger.info(f"Found {len(vpc_attachments)} VPC attachment(s)")

    all_in_sync = True
    total_checked = 0
    total_synced = 0

    # Step 3: For each VPC, check and sync tagged route tables
    for attachment in vpc_attachments:
        vpc_id = attachment['vpc_id']
        attachment_id = attachment['attachment_id']

        logger.info(f"Checking VPC {vpc_id} (attachment: {attachment_id})")

        # Get tagged route tables in this VPC
        vpc_route_table_ids = get_tagged_vpc_route_tables(vpc_id, region)

        if not vpc_route_table_ids:
            logger.info(f"  No tagged route tables in VPC {vpc_id}")
            continue

        logger.info(f"  Found {len(vpc_route_table_ids)} tagged route table(s)")

        for vpc_rt_id in vpc_route_table_ids:
            total_checked += 1
            logger.info(f"  Checking VPC route table: {vpc_rt_id}")

            # Get current routes in VPC route table
            vpc_routes = get_vpc_routes(vpc_rt_id, region)

            # Filter to only routes pointing to THIS TGW
            vpc_tgw_routes = {
                cidr: details for cidr, details in vpc_routes.items()
                if details.get('target_type') == 'transit-gateway'
                and details.get('target_id') == tgw_id
            }
            vpc_tgw_cidrs = set(vpc_tgw_routes.keys())

            logger.info(f"    VPC RT has {len(vpc_tgw_cidrs)} routes pointing to this TGW")

            # Step 4: Compare TGW routes (source) vs VPC routes (target)
            cidrs_to_add = tgw_cidrs - vpc_tgw_cidrs  # In TGW but not in VPC
            cidrs_to_remove = vpc_tgw_cidrs - tgw_cidrs  # In VPC but not in TGW

            if not cidrs_to_add and not cidrs_to_remove:
                # Routes match - FALSE POSITIVE (no action needed)
                logger.info(f"    ✓ VPC RT {vpc_rt_id} is IN SYNC with TGW")
                continue

            # Routes don't match - SYNC IMMEDIATELY (NO COOLDOWN)
            all_in_sync = False
            total_synced += 1

            logger.info(f"    ✗ VPC RT {vpc_rt_id} is OUT OF SYNC - syncing now")
            logger.info(f"      Routes to ADD: {len(cidrs_to_add)}")
            logger.info(f"      Routes to REMOVE: {len(cidrs_to_remove)}")

            # Step 5: ADD missing routes
            for cidr in sorted(cidrs_to_add):
                # Check for overlap with other targets before adding
                if cidr in vpc_routes:
                    existing = vpc_routes[cidr]
                    if existing.get('target_type') != 'transit-gateway' or existing.get('target_id') != tgw_id:
                        logger.warning(f"      OVERLAP: {cidr} exists via "
                                       f"{existing.get('target_type')}:{existing.get('target_id')} - skipping")
                        continue

                if add_vpc_route(vpc_rt_id, cidr, tgw_id, region):
                    result['routes_added'] += 1
                    logger.info(f"      ADDED: {cidr} -> {tgw_id}")

            # Step 6: REMOVE stale routes
            for cidr in sorted(cidrs_to_remove):
                if remove_vpc_route(vpc_rt_id, cidr, region):
                    result['routes_removed'] += 1
                    logger.info(f"      REMOVED: {cidr}")

    result['vpc_route_tables_checked'] = total_checked
    result['vpc_route_tables_synced'] = total_synced

    # Step 7: Update DynamoDB state (for audit trail only)
    if all_in_sync:
        logger.info(f"All VPC route tables are in sync - no changes made")
        result['skipped'] = True
        result['skip_reason'] = 'All VPC route tables already in sync'

        update_state_checked(
            tgw_id=tgw_id,
            tgw_route_table_id=tgw_route_table_id,
            tgw_cidrs=tgw_cidrs,
            region=region,
            global_network_id=global_network_id,
            was_in_sync=True
        )
    else:
        update_state_synced(
            tgw_id=tgw_id,
            tgw_route_table_id=tgw_route_table_id,
            tgw_cidrs=tgw_cidrs,
            region=region,
            global_network_id=global_network_id,
            routes_added=result['routes_added'],
            routes_removed=result['routes_removed']
        )

    return result

def get_tgw_route_tables(tgw_id: str, region: str) -> List[str]:
    """Get all route tables associated with a Transit Gateway."""
    route_tables = []
    ec2 = get_ec2_client(region)

    try:
        paginator = ec2.get_paginator('describe_transit_gateway_route_tables')

        for page in paginator.paginate(
            Filters=[{'Name': 'transit-gateway-id', 'Values': [tgw_id]}]
        ):
            for rt in page.get('TransitGatewayRouteTables', []):
                if rt.get('State') == 'available':
                    route_tables.append(rt['TransitGatewayRouteTableId'])
                    logger.debug(f"Found TGW route table: {rt['TransitGatewayRouteTableId']}")

    except ClientError as e:
        logger.error(f"Error getting route tables for TGW {tgw_id} in {region}: {str(e)}")
        raise

    return route_tables

def get_tgw_routes(tgw_route_table_id: str, region: str) -> Dict[str, Dict]:
    """Get all active routes from a TGW route table."""
    routes = {}
    ec2 = get_ec2_client(region)

    try:
        response = ec2.search_transit_gateway_routes(
            TransitGatewayRouteTableId=tgw_route_table_id,
            Filters=[{'Name': 'state', 'Values': ['active']}],
            MaxResults=1000
        )

        for route in response.get('Routes', []):
            cidr = route.get('DestinationCidrBlock')
            if cidr:
                routes[cidr] = {
                    'type': route.get('Type'),
                    'state': route.get('State'),
                    'attachments': route.get('TransitGatewayAttachments', [])
                }

    except ClientError as e:
        logger.error(f"Error getting routes for {tgw_route_table_id} in {region}: {str(e)}")
        raise

    return routes

def get_vpc_attachments(tgw_route_table_id: str, region: str) -> List[Dict[str, str]]:
    """Get VPC attachments associated with a TGW route table."""
    attachments = []
    ec2 = get_ec2_client(region)

    try:
        paginator = ec2.get_paginator('get_transit_gateway_route_table_associations')

        for page in paginator.paginate(TransitGatewayRouteTableId=tgw_route_table_id):
            for assoc in page.get('Associations', []):
                if assoc.get('ResourceType') == 'vpc' and assoc.get('State') == 'associated':
                    attachment_id = assoc['TransitGatewayAttachmentId']
                    vpc_id = get_vpc_id_from_attachment(attachment_id, region)

                    if vpc_id:
                        attachments.append({
                            'vpc_id': vpc_id,
                            'attachment_id': attachment_id
                        })
                        logger.debug(f"Found VPC attachment: {vpc_id} ({attachment_id})")

    except ClientError as e:
        logger.error(f"Error getting VPC attachments for {tgw_route_table_id}: {str(e)}")
        raise

    return attachments

def get_vpc_id_from_attachment(attachment_id: str, region: str) -> Optional[str]:
    """Get VPC ID from a TGW attachment."""
    ec2 = get_ec2_client(region)

    try:
        response = ec2.describe_transit_gateway_vpc_attachments(
            TransitGatewayAttachmentIds=[attachment_id]
        )

        attachments = response.get('TransitGatewayVpcAttachments', [])
        if attachments:
            return attachments[0].get('VpcId')

    except ClientError as e:
        logger.error(f"Error getting VPC from attachment {attachment_id}: {str(e)}")

    return None

def get_tagged_vpc_route_tables(vpc_id: str, region: str) -> List[str]:
    """Get route tables in a VPC that are tagged with TGWRouteSync=enabled."""
    route_table_ids = []
    ec2 = get_ec2_client(region)

    try:
        paginator = ec2.get_paginator('describe_route_tables')

        for page in paginator.paginate(
            Filters=[
                {'Name': 'vpc-id', 'Values': [vpc_id]},
                {'Name': 'tag:TGWRouteSync', 'Values': ['enabled', 'true', 'yes']}
            ]
        ):
            for rt in page.get('RouteTables', []):
                route_table_ids.append(rt['RouteTableId'])
                logger.debug(f"Found tagged route table: {rt['RouteTableId']} in VPC {vpc_id}")

    except ClientError as e:
        logger.error(f"Error getting route tables for VPC {vpc_id}: {str(e)}")
        raise

    return route_table_ids

def get_vpc_routes(route_table_id: str, region: str) -> Dict[str, Dict]:
    """Get all routes from a VPC route table with target information."""
    routes = {}
    ec2 = get_ec2_client(region)

    try:
        response = ec2.describe_route_tables(
            RouteTableIds=[route_table_id]
        )

        for rt in response.get('RouteTables', []):
            for route in rt.get('Routes', []):
                cidr = route.get('DestinationCidrBlock')
                if not cidr:
                    continue

                target_type, target_id = parse_route_target(route)

                routes[cidr] = {
                    'target_type': target_type,
                    'target_id': target_id,
                    'state': route.get('State'),
                    'origin': route.get('Origin')
                }

    except ClientError as e:
        logger.error(f"Error getting routes for {route_table_id}: {str(e)}")
        raise

    return routes

def parse_route_target(route: Dict) -> tuple:
    """Parse route target type and ID from a route entry."""
    if route.get('TransitGatewayId'):
        return ('transit-gateway', route['TransitGatewayId'])
    elif route.get('GatewayId'):
        gw_id = route['GatewayId']
        if gw_id == 'local':
            return ('local', 'local')
        elif gw_id.startswith('igw-'):
            return ('internet-gateway', gw_id)
        elif gw_id.startswith('vgw-'):
            return ('vpn-gateway', gw_id)
        else:
            return ('gateway', gw_id)
    elif route.get('NatGatewayId'):
        return ('nat-gateway', route['NatGatewayId'])
    elif route.get('NetworkInterfaceId'):
        return ('network-interface', route['NetworkInterfaceId'])
    elif route.get('VpcPeeringConnectionId'):
        return ('vpc-peering', route['VpcPeeringConnectionId'])
    elif route.get('EgressOnlyInternetGatewayId'):
        return ('egress-only-igw', route['EgressOnlyInternetGatewayId'])
    elif route.get('InstanceId'):
        return ('instance', route['InstanceId'])
    else:
        return ('unknown', 'unknown')

def update_state_checked(tgw_id: str, tgw_route_table_id: str, tgw_cidrs: Set[str],
                         region: str, global_network_id: str, was_in_sync: bool):
    """
    Update DynamoDB state when routes were checked (for audit trail).
    """
    now = datetime.now(timezone.utc)
    ttl = int((now + timedelta(days=30)).timestamp())

    try:
        table.put_item(
            Item={
                'tgw_id': tgw_id,
                'tgw_route_table_id': tgw_route_table_id,
                'tgw_routes': list(tgw_cidrs),
                'last_check_timestamp': now.isoformat(),
                'route_count': len(tgw_cidrs),
                'region': region,
                'global_network_id': global_network_id,
                'last_check_result': 'IN_SYNC' if was_in_sync else 'CHECKED',
                'ttl': ttl
            }
        )
        logger.debug(f"Updated DynamoDB state (checked) for {tgw_id}/{tgw_route_table_id}")

    except ClientError as e:
        logger.error(f"Error updating DynamoDB (checked): {str(e)}")

def update_state_synced(tgw_id: str, tgw_route_table_id: str, tgw_cidrs: Set[str],
                        region: str, global_network_id: str,
                        routes_added: int, routes_removed: int):
    """Update DynamoDB state after successful sync (for audit trail)."""
    now = datetime.now(timezone.utc)
    ttl = int((now + timedelta(days=30)).timestamp())

    try:
        table.put_item(
            Item={
                'tgw_id': tgw_id,
                'tgw_route_table_id': tgw_route_table_id,
                'tgw_routes': list(tgw_cidrs),
                'last_sync_timestamp': now.isoformat(),
                'last_check_timestamp': now.isoformat(),
                'route_count': len(tgw_cidrs),
                'region': region,
                'global_network_id': global_network_id,
                'last_sync_result': 'SUCCESS',
                'last_check_result': 'SYNCED',
                'routes_added': routes_added,
                'routes_removed': routes_removed,
                'ttl': ttl
            }
        )
        logger.info(f"Updated DynamoDB state (synced) for {tgw_id}/{tgw_route_table_id}: "
                    f"added={routes_added}, removed={routes_removed}")

    except ClientError as e:
        logger.error(f"Error updating DynamoDB (synced): {str(e)}")

def add_vpc_route(route_table_id: str, cidr: str, tgw_id: str, region: str) -> bool:
    """Add a route to a VPC route table pointing to the TGW."""
    ec2 = get_ec2_client(region)

    try:
        ec2.create_route(
            RouteTableId=route_table_id,
            DestinationCidrBlock=cidr,
            TransitGatewayId=tgw_id
        )
        return True

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == 'RouteAlreadyExists':
            # Route already exists - this is okay
            return True
        logger.error(f"Error adding route {cidr} to {route_table_id}: {str(e)}")
        return False

def remove_vpc_route(route_table_id: str, cidr: str, region: str) -> bool:
    """Remove a route from a VPC route table."""
    ec2 = get_ec2_client(region)

    try:
        ec2.delete_route(
            RouteTableId=route_table_id,
            DestinationCidrBlock=cidr
        )
        return True

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == 'InvalidRoute.NotFound':
            # Route doesn't exist - this is okay
            return True
        logger.error(f"Error removing route {cidr} from {route_table_id}: {str(e)}")
        return False
